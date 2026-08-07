"""The capture loop: owns the serial port, writes the session.

Hard-won behaviors (each traces to a real field incident):

- **DTR asserted on open.** ESP32-S3 native USB (HWCDC) drops ALL device TX
  until the host asserts DTR — ``cat /dev/cu.usbmodemX`` sees nothing and
  misdirects debugging for hours.
- **Exclusive open + single owner.** Two readers on one port corrupt both.
  Flashing tools need the port too, so the loop supports pause/resume.
- **Re-resolve the port on every reconnect.** USB re-enumeration renumbers
  ports (``usbmodem101`` → ``usbmodem1101``); a stored path goes stale.
- **Reopen backoff with a holdoff cap.** Instant reopen after USB
  re-enumeration lands on a half-dead fd; a ~15s ceiling is field-proven.
- **Never a silent gap.** Port loss, port-missing, and reopen are all written
  to the session as status records — a 52-minute unexplained hole in the log
  is how you lose an afternoon.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import serial

from . import ports
from .crash import CrashAssembler, decode_backtrace, find_addr2line
from .parsers import parse_line
from .records import Event, LogRecord, now_iso
from .session import SessionWriter

BOOT_DEBOUNCE_S = 3.0     # ROM emits several marker lines per reset; count once
REOPEN_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 15.0)  # then stay at 15s


def default_serial_factory(device: str, baud: int) -> serial.Serial:
    ser = serial.Serial()
    ser.port = device
    ser.baudrate = baud
    ser.timeout = 0.2
    ser.dtr = True  # assert DTR *at* open: HWCDC gates TX on it
    ser.rts = False
    try:
        ser.exclusive = True  # POSIX; harmless elsewhere
    except ValueError:
        pass
    ser.open()
    return ser


class CaptureLoop:
    """Read the port, structure every line, survive disconnects.

    ``on_line`` (optional) receives each formatted record — used by the
    foreground monitor for live echo. Tests inject ``serial_factory``.
    """

    def __init__(
        self,
        writer: SessionWriter,
        port_spec: str | None = None,
        baud: int = 115200,
        serial_factory: Callable[[str, int], serial.Serial] = default_serial_factory,
        on_line: Callable[[LogRecord], None] | None = None,
    ) -> None:
        self.writer = writer
        self.port_spec = port_spec
        self.baud = baud
        self.serial_factory = serial_factory
        self.on_line = on_line
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self._crash = CrashAssembler()
        self._boot = 0
        self._last_boot_mark = 0.0
        self._ser: serial.Serial | None = None
        self._buf = b""

    # -- public control (called from daemon/control thread) -----------------

    def stop(self) -> None:
        self.stop_event.set()

    def pause(self) -> None:
        """Release the port (e.g. so esptool can flash) without ending the session."""
        self.pause_event.set()

    def resume(self) -> None:
        self.pause_event.clear()

    def send(self, data: bytes) -> bool:
        ser = self._ser
        if ser is None or not ser.is_open:
            return False
        try:
            ser.write(data)
            self._record(Event.SENT, msg=data.decode("utf-8", "replace").rstrip("\n"))
            return True
        except (serial.SerialException, OSError):
            return False

    @property
    def connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    # -- main loop -----------------------------------------------------------

    def run(self) -> None:
        backoff_idx = 0
        try:
            while not self.stop_event.is_set():
                if self.pause_event.is_set():
                    self._close_port(reason="paused for flash")
                    self.stop_event.wait(0.2)
                    continue

                if self._ser is None:
                    if not self._open_port():
                        delay = REOPEN_BACKOFF_S[min(backoff_idx, len(REOPEN_BACKOFF_S) - 1)]
                        backoff_idx += 1
                        self.stop_event.wait(delay)
                        continue
                    backoff_idx = 0

                try:
                    data = self._ser.read(self._ser.in_waiting or 1)
                except (serial.SerialException, OSError):
                    self._on_port_lost()
                    continue
                if data:
                    self.writer.write_raw(data)
                    self._buf += data
                    self._drain_lines()
        finally:
            self._close_port(reason="capture stopped")
            self.writer.close()

    # -- internals -----------------------------------------------------------

    def _open_port(self) -> bool:
        board = ports.resolve_port(self.port_spec)
        if board is None:
            self._record(Event.STATUS, msg=f"port not found (spec={self.port_spec or 'auto'})")
            return False
        try:
            self._ser = self.serial_factory(board.device, self.baud)
        except (serial.SerialException, OSError) as e:
            self._record(Event.STATUS, msg=f"open failed on {board.device}: {e}")
            self._ser = None
            return False
        self._buf = b""
        self._record(
            Event.STATUS,
            msg=f"port opened: {board.device} @ {self.baud}",
            extra={"device": board.device, "hint": board.hint},
        )
        return True

    def _on_port_lost(self) -> None:
        report = self._crash.flush()
        if report is not None:
            self._emit_crash(report)
        self._close_port(reason="port lost (device reset or unplugged)")

    def _close_port(self, reason: str) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except (serial.SerialException, OSError):
                pass
            self._ser = None
            self._record(Event.STATUS, msg=reason)

    def _drain_lines(self) -> None:
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            self._handle_line(raw.decode("utf-8", "replace"))
        if len(self._buf) > 8192:  # unterminated garbage guard (binary spew)
            self._handle_line(self._buf.decode("utf-8", "replace"))
            self._buf = b""

    def _handle_line(self, raw: str) -> None:
        parsed = parse_line(raw)
        if not parsed.msg and not parsed.is_boot_marker:
            return

        if parsed.is_boot_marker:
            now = time.monotonic()
            if now - self._last_boot_mark > BOOT_DEBOUNCE_S:
                self._boot = self.writer.note_boot()
                self._record(Event.BOOT, msg=parsed.msg)
                self._last_boot_mark = now
                # fall through: the marker line itself is also worth keeping
            else:
                self._last_boot_mark = now

        self._record(
            Event.LOG,
            msg=parsed.msg,
            level=parsed.level,
            tag=parsed.tag,
            dev_ts=parsed.dev_ts,
            src=parsed.src,
        )

        report = self._crash.feed(parsed.msg)
        if report is not None:
            self._emit_crash(report)

    def _emit_crash(self, report) -> None:
        if report.backtrace_addrs and self.writer.meta.elf_paths:
            report.decoded_frames = decode_backtrace(
                report.backtrace_addrs,
                self.writer.meta.elf_paths[-1],
                find_addr2line(),
            )
        path = self.writer.write_crash(report)
        self._record(
            Event.CRASH,
            msg=report.summary(),
            level="E",
            extra={"crash_id": report.crash_id, "file": str(path)},
        )

    def _record(
        self,
        event: str,
        msg: str,
        level: str | None = None,
        tag: str | None = None,
        dev_ts: int | None = None,
        src: str | None = None,
        extra: dict | None = None,
    ) -> None:
        rec = LogRecord(
            ts=now_iso(),
            seq=self.writer.next_seq(),
            boot=self._boot,
            event=event,
            level=level,
            tag=tag,
            msg=msg,
            dev_ts=dev_ts,
            src=src,
            extra=extra,
        )
        self.writer.write_record(rec)
        if self.on_line is not None:
            self.on_line(rec)
