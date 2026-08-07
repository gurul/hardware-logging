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

import contextlib
import fcntl
import os
import stat
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import serial

from . import ports
from .crash import CrashAssembler, CrashReport, decode_backtrace, find_addr2line
from .parsers import parse_line
from .records import Event, LogRecord, now_iso
from .session import SessionWriter, base_dir, ensure_private_dir, port_slug

BOOT_DEBOUNCE_S = 3.0  # ROM emits several marker lines per reset; count once
REOPEN_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 15.0)  # then stay at 15s
MAX_STRUCTURED_LINE_BYTES = 8192
MAX_SEND_BYTES = 4096
MAX_PENDING_SYMBOLIZATIONS = 4
MAX_UNRESOLVED_CRASHES = 32


def default_serial_factory(device: str, baud: int) -> serial.Serial:
    ser = serial.Serial()
    ser.port = device
    ser.baudrate = baud
    ser.timeout = 0.2
    ser.write_timeout = 2.0
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
        self._wake_event = threading.Event()
        self._port_released = threading.Event()
        self._port_released.set()
        self._crash = CrashAssembler()
        self._boot = 0
        self._last_boot_mark = 0.0
        self._ser: serial.Serial | None = None
        self._buf = b""
        self._usb_serial: str | None = writer.meta.usb_serial
        self._usb_location: str | None = writer.meta.usb_location
        self._usb_vid: int | None = writer.meta.usb_vid
        self._usb_pid: int | None = writer.meta.usb_pid
        self._decoder = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hwlog-symbolize")
        self._decode_slots = threading.BoundedSemaphore(MAX_PENDING_SYMBOLIZATIONS)
        self._symbolization_lock = threading.Lock()
        self._unresolved_crashes: dict[int, tuple[int, CrashReport, Path]] = {}
        self._firmware_generation = writer.meta.firmware_generation
        self._active_elf: str | None = (
            writer.meta.elf_paths[-1]
            if (
                writer.meta.elf_paths
                and not writer.meta.elf_pending
                and writer.meta.elf_generation == writer.meta.firmware_generation
            )
            else None
        )
        self._active_elf_generation = (
            writer.meta.elf_generation if self._active_elf is not None else None
        )
        self._device_locks: dict[str, object] = {}

    # -- public control (called from daemon/control thread) -----------------

    def stop(self) -> None:
        self.stop_event.set()
        self._wake_event.set()

    def dispose(self) -> None:
        """Release background resources for a loop that never ran.

        run() performs this teardown itself on exit; dispose() exists for
        owners that constructed a loop and failed before starting it, so they
        never need to reach into private members.
        """
        try:
            self._decoder.shutdown(wait=False, cancel_futures=True)
        finally:
            self._release_device_ownership()

    def pause(self, timeout: float | None = None) -> bool:
        """Request a port release and optionally wait for the close acknowledgment."""
        self.pause_event.set()
        self._wake_event.set()
        return True if timeout is None else self._port_released.wait(timeout)

    def resume(
        self,
        *,
        awaiting_elf: bool = False,
        record_flash_boundary: bool = False,
    ) -> int:
        with self._symbolization_lock:
            if record_flash_boundary or awaiting_elf:
                generation = self.writer.mark_flash_boundary(awaiting_elf=awaiting_elf)
            else:
                generation = self._firmware_generation
            if awaiting_elf:
                # A flash attempt must never be symbolized against the previous
                # firmware. Crashes remain pending until its exact ELF is ready.
                self._firmware_generation = generation
                self._active_elf = None
                self._active_elf_generation = None
                self._unresolved_crashes = {
                    crash_id: pending
                    for crash_id, pending in self._unresolved_crashes.items()
                    if pending[0] == generation
                }
        self.pause_event.clear()
        self._wake_event.set()
        return generation

    def add_elf(self, elf_path: str, *, generation: int | None = None) -> None:
        """Activate a newly archived ELF and decode any crash from the archive gap."""
        require_pending = generation is not None
        expected_generation = self._firmware_generation if generation is None else generation
        with self._symbolization_lock:
            if expected_generation != self._firmware_generation:
                raise ValueError("ELF belongs to a stale firmware generation")
            self.writer.add_elf(
                elf_path,
                generation=expected_generation,
                require_pending=require_pending,
            )
            self._active_elf = elf_path
            self._active_elf_generation = self.writer.meta.elf_generation
            self._unresolved_crashes = {
                crash_id: pending
                for crash_id, pending in self._unresolved_crashes.items()
                if pending[0] == self._active_elf_generation
            }
        self._drain_symbolizations()

    def send(self, data: bytes) -> bool:
        if len(data) > MAX_SEND_BYTES:
            return False
        ser = self._ser
        if ser is None or not ser.is_open:
            return False
        try:
            ser.write(data)
            # Sent commands often contain provisioning secrets. Record the action,
            # not the payload; the caller already knows what it sent.
            self._record(Event.SENT, msg=f"sent {len(data)} bytes", extra={"bytes": len(data)})
            return True
        except (serial.SerialException, serial.SerialTimeoutException, OSError):
            return False

    def note(self, message: str) -> None:
        """Record a host-side lifecycle note through the sequenced writer."""
        self._record(Event.STATUS, msg=message)

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
                    self._wait_for_control(0.2)
                    continue

                if self._ser is None:
                    if not self._open_port():
                        if self.pause_event.is_set():
                            continue
                        delay = REOPEN_BACKOFF_S[min(backoff_idx, len(REOPEN_BACKOFF_S) - 1)]
                        backoff_idx += 1
                        self._wait_for_control(delay)
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
            try:
                self._close_port(reason="capture stopped")
            finally:
                try:
                    self.writer.close()
                finally:
                    try:
                        self._decoder.shutdown(wait=False, cancel_futures=True)
                    finally:
                        self._release_device_ownership()

    # -- internals -----------------------------------------------------------

    def _open_port(self) -> bool:
        # Mark an open attempt as unreleased before doing discovery/metadata I/O;
        # otherwise a concurrent pause could observe the initial "released" state,
        # acknowledge the flasher, and then let this attempt open the port.
        self._port_released.clear()
        try:
            board = ports.resolve_port(self.port_spec)
        except ports.AmbiguousPortError as exc:
            # Once a physical USB identity is pinned, added boards must not make
            # reconnect ambiguous. Follow only that identity and never switch.
            board = self._resolve_pinned_board() if self._has_pinned_identity() else None
            if board is None:
                self._record(Event.STATUS, msg=str(exc))
                self._port_released.set()
                return False
        if board is None and self._has_pinned_identity():
            board = self._resolve_pinned_board()
        elif (
            board is not None
            and self._has_pinned_identity()
            and not self._board_matches_pinned(board)
        ):
            # An explicit OS path can be reused by another USB device after the
            # pinned board re-enumerates. Prefer the pinned identity when unique.
            pinned = self._resolve_pinned_board()
            if pinned is not None:
                board = pinned
        if board is None:
            self._record(Event.STATUS, msg=f"port not found (spec={self.port_spec or 'auto'})")
            self._port_released.set()
            return False
        board_serial = getattr(board, "serial_number", None)
        board_location = getattr(board, "location", None)
        board_vid = getattr(board, "vid", None)
        board_pid = getattr(board, "pid", None)
        if self._usb_serial is not None and board_serial != self._usb_serial:
            self._record(
                Event.STATUS,
                msg=(
                    "refusing to switch physical boards after reconnect "
                    f"(expected USB serial {self._usb_serial}, "
                    f"found {board_serial or 'unavailable'})"
                ),
            )
            self._port_released.set()
            return False
        if (
            self._usb_serial is None
            and self._usb_location is not None
            and board_location != self._usb_location
        ):
            self._record(
                Event.STATUS,
                msg=(
                    "refusing to switch USB topology after reconnect "
                    f"(expected {self._usb_location}, found {board_location or 'unavailable'})"
                ),
            )
            self._port_released.set()
            return False
        if (
            self._usb_serial is None
            and self._usb_location is None
            and self._usb_vid is not None
            and board_vid is not None
            and (board_vid, board_pid) != (self._usb_vid, self._usb_pid)
        ):
            self._record(
                Event.STATUS,
                msg=(
                    "refusing to switch USB device type after reconnect "
                    f"(expected {self._usb_vid:04x}:{self._usb_pid or 0:04x}, "
                    f"found {board_vid:04x}:{board_pid or 0:04x})"
                ),
            )
            self._port_released.set()
            return False
        identity_was_pinned = self._has_pinned_identity()
        if not self._acquire_device_ownership(board):
            self._record(
                Event.STATUS,
                msg=f"another hwlog process owns physical device {board.device}",
            )
            self._port_released.set()
            return False
        if not identity_was_pinned:
            # Choose the strongest identity available on first ownership and keep
            # that identity aligned with the lifetime lock. A later metadata
            # upgrade must not silently move this session to a different lock
            # namespace while it still holds the original lock.
            self._usb_serial = board_serial
            self._usb_location = board_location
            self._usb_vid, self._usb_pid = board_vid, board_pid
        self.writer.note_board(
            vid=self._usb_vid,
            pid=self._usb_pid,
            serial_number=self._usb_serial,
            hint=getattr(board, "hint", None),
            location=self._usb_location,
        )
        if self.pause_event.is_set():
            self._port_released.set()
            return False
        try:
            self._ser = self.serial_factory(board.device, self.baud)
        except (serial.SerialException, OSError, ValueError) as e:
            self._record(Event.STATUS, msg=f"open failed on {board.device}: {e}")
            self._ser = None
            self._port_released.set()
            return False
        self._buf = b""
        self._record(
            Event.STATUS,
            msg=f"port opened: {board.device} @ {self.baud}",
            extra={"device": board.device, "hint": board.hint},
        )
        return True

    def _resolve_pinned_board(self):
        """Follow the same physical USB device when its OS path is renumbered."""
        discovered = ports.discover()
        if self._usb_serial:
            matches = [b for b in discovered if b.serial_number == self._usb_serial]
        elif self._usb_location:
            matches = [b for b in discovered if b.location == self._usb_location]
        else:
            matches = [b for b in discovered if b.vid == self._usb_vid and b.pid == self._usb_pid]
        return matches[0] if len(matches) == 1 else None

    def _has_pinned_identity(self) -> bool:
        return (
            self._usb_serial is not None
            or self._usb_location is not None
            or self._usb_vid is not None
        )

    def _board_matches_pinned(self, board) -> bool:
        board_serial = getattr(board, "serial_number", None)
        if self._usb_serial is not None:
            return board_serial == self._usb_serial
        if self._usb_location is not None:
            return getattr(board, "location", None) == self._usb_location
        return (getattr(board, "vid", None), getattr(board, "pid", None)) == (
            self._usb_vid,
            self._usb_pid,
        )

    def _wait_for_control(self, timeout: float) -> None:
        self._wake_event.wait(timeout)
        self._wake_event.clear()

    def _acquire_device_ownership(self, board) -> bool:
        """Hold a physical-device lock across reconnects and flash pauses."""
        serial_number = getattr(board, "serial_number", None)
        location = getattr(board, "location", None)
        vid = getattr(board, "vid", None)
        pid = getattr(board, "pid", None)
        if self._device_locks:
            # The board already passed pinned-identity validation. Retain the
            # lifetime lock chosen on first ownership rather than attempting an
            # unsafe lock-key upgrade when the OS exposes stronger metadata.
            return self._board_matches_pinned(board)
        pinned = self._has_pinned_identity()
        strong_identities = []
        pinned_serial = self._usb_serial if pinned else serial_number
        pinned_location = self._usb_location if pinned else location
        pinned_vid = self._usb_vid if pinned else vid
        pinned_pid = self._usb_pid if pinned else pid
        if pinned_serial:
            strong_identities.append(f"usb-serial:{pinned_serial}")
        if pinned_location:
            strong_identities.append(f"usb-location:{pinned_location}")
        specs = [(port_slug(identity), fcntl.LOCK_EX) for identity in strong_identities]
        if pinned_vid is not None:
            # Strong identities may coexist for distinct serialized boards of
            # the same type, but take a shared VID/PID lock so an older weak-only
            # owner (which takes it exclusively) still conflicts.
            mode = fcntl.LOCK_SH if strong_identities else fcntl.LOCK_EX
            specs.append((port_slug(f"usb:{pinned_vid}:{pinned_pid}"), mode))
        if not specs:
            specs.append((port_slug(f"device:{board.device}"), fcntl.LOCK_EX))
        specs = sorted(set(specs))
        lock_dir = ensure_private_dir(base_dir() / "device-locks")
        acquired: dict[str, object] = {}
        try:
            for key, mode in specs:
                path = lock_dir / f"{key}.lock"
                flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, flags, 0o600)
                lock = None
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                        raise OSError(f"refusing unsafe physical-device lock: {path}")
                    os.fchmod(fd, 0o600)
                    lock = os.fdopen(fd, "a+")
                    fd = -1
                    fcntl.flock(lock.fileno(), mode | fcntl.LOCK_NB)
                except BaseException:
                    if lock is not None:
                        with contextlib.suppress(OSError):
                            lock.close()
                    raise
                finally:
                    if fd >= 0:
                        os.close(fd)
                acquired[key] = lock
        except (BlockingIOError, OSError):
            for lock in acquired.values():
                with contextlib.suppress(OSError):
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                with contextlib.suppress(OSError):
                    lock.close()
            return False
        self._device_locks = acquired
        return True

    def _release_device_ownership(self) -> None:
        locks, self._device_locks = self._device_locks, {}
        for lock in locks.values():
            with contextlib.suppress(OSError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                lock.close()

    def _on_port_lost(self) -> None:
        self._close_port(reason="port lost (device reset or unplugged)")

    def _close_port(self, reason: str) -> None:
        ser = self._ser
        if ser is not None:
            try:
                self._finalize_stream()
            finally:
                try:
                    ser.close()
                except (serial.SerialException, OSError):
                    pass
                self._ser = None
                self._port_released.set()
            self._record(Event.STATUS, msg=reason)
        else:
            self._port_released.set()

    def _finalize_stream(self) -> None:
        if self._buf:
            pending, self._buf = self._buf, b""
            self._handle_bytes(pending)
        report = self._crash.flush()
        if report is not None:
            self._emit_crash(report)

    def _drain_lines(self) -> None:
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            self._handle_bytes(raw)
        while len(self._buf) > MAX_STRUCTURED_LINE_BYTES:
            raw, self._buf = (
                self._buf[:MAX_STRUCTURED_LINE_BYTES],
                self._buf[MAX_STRUCTURED_LINE_BYTES:],
            )
            self._handle_bytes(raw)

    def _handle_bytes(self, raw: bytes) -> None:
        if not raw:
            return
        for start in range(0, len(raw), MAX_STRUCTURED_LINE_BYTES):
            chunk = raw[start : start + MAX_STRUCTURED_LINE_BYTES]
            self._handle_line(chunk.decode("utf-8", "replace"))

    def _handle_line(self, raw: str) -> None:
        parsed = parse_line(raw)
        if not parsed.msg and not parsed.is_boot_marker:
            return

        # A reboot marker may also terminate a crash. Finish the old crash before
        # incrementing the boot so it is attributed to the boot that actually failed.
        report = self._crash.feed(parsed.msg)
        if report is not None:
            self._emit_crash(report)

        if parsed.is_boot_marker:
            now = time.monotonic()
            if now - self._last_boot_mark > BOOT_DEBOUNCE_S:
                self._boot = self.writer.note_boot()
                self._record(Event.BOOT, msg=parsed.msg)
                self._last_boot_mark = now
                # fall through: the marker line itself is also worth keeping

        self._record(
            Event.LOG,
            msg=parsed.msg,
            level=parsed.level,
            tag=parsed.tag,
            dev_ts=parsed.dev_ts,
            src=parsed.src,
        )

    def _emit_crash(self, report: CrashReport) -> None:
        path = self.writer.write_crash(report)
        extra = {"crash_id": report.crash_id}
        if path is not None:
            extra["file"] = str(path)
        self._record(
            Event.CRASH,
            msg=report.summary(),
            level="E",
            extra=extra,
        )
        # A quota-dropped artifact is gone; symbolizing it would recreate the
        # file via update_crash and silently bypass the drop decision.
        if path is not None and report.backtrace_addrs:
            with self._symbolization_lock:
                self._unresolved_crashes[report.crash_id] = (
                    self._firmware_generation,
                    report,
                    path,
                )
                while len(self._unresolved_crashes) > MAX_UNRESOLVED_CRASHES:
                    # Evict the NEWEST pending crash: in a crash loop the
                    # first crash is the root cause and the tail repeats it.
                    self._unresolved_crashes.popitem()
            self._drain_symbolizations()

    def _drain_symbolizations(self) -> None:
        tool = find_addr2line()
        if tool is None:
            return
        while self._decode_slots.acquire(blocking=False):
            with self._symbolization_lock:
                if self._active_elf is None or not self._unresolved_crashes:
                    self._decode_slots.release()
                    return
                # Decode oldest-first, matching the eviction policy above.
                oldest = next(iter(self._unresolved_crashes))
                generation, report, path = self._unresolved_crashes.pop(oldest)
                if generation != self._active_elf_generation:
                    self._decode_slots.release()
                    continue
                elf_path = self._active_elf
            try:
                future = self._decoder.submit(self._symbolize_crash, report, path, elf_path, tool)
            except RuntimeError:
                self._decode_slots.release()
                return
            future.add_done_callback(self._symbolization_done)

    def _symbolization_done(self, _future) -> None:
        self._decode_slots.release()
        self._drain_symbolizations()

    def _symbolize_crash(
        self,
        report: CrashReport,
        path: Path,
        elf_path: str,
        tool: str,
    ) -> None:
        frames = decode_backtrace(report.backtrace_addrs, elf_path, tool)
        if frames:
            report.decoded_frames = frames
            try:
                self.writer.update_crash(path, report)
            except OSError:
                pass

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
            seq=0,
            boot=self._boot,
            event=event,
            level=level,
            tag=tag,
            msg=msg,
            dev_ts=dev_ts,
            src=src,
            extra=extra,
        )
        self.writer.append_record(rec)
        if self.on_line is not None:
            self.on_line(rec)
