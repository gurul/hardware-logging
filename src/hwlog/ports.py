"""Serial port discovery and board identification.

USB VIDs identify the bridge chip (or native-USB MCU), which is usually enough
to say "that's the dev board" among a sea of ``/dev/cu.*`` noise. Ports
renumber on replug (``usbmodem101`` → ``usbmodem1101``), so discovery re-runs
on every reconnect rather than trusting a stored path.
"""

from __future__ import annotations

from dataclasses import dataclass

from serial.tools import list_ports

VID_HINTS: dict[int, str] = {
    0x303A: "Espressif (native USB — ESP32-S2/S3/C3/C6)",
    0x10C4: "Silicon Labs CP210x bridge (common on ESP32 devkits)",
    0x1A86: "QinHeng CH340/CH9102 bridge (common on clones/NodeMCU)",
    0x0403: "FTDI bridge",
    0x2E8A: "Raspberry Pi (RP2040/RP2350)",
    0x0483: "STMicroelectronics (STM32)",
    0x1915: "Nordic Semiconductor (nRF)",
    0x2341: "Arduino",
    0x239A: "Adafruit",
    0x1B4F: "SparkFun",
    0x0D28: "ARM mbed/DAPLink",
}


class AmbiguousPortError(ValueError):
    """A selector matches more than one live serial device."""


@dataclass
class Board:
    device: str
    description: str
    vid: int | None
    pid: int | None
    serial_number: str | None
    hint: str | None
    location: str | None = None

    @property
    def is_known(self) -> bool:
        return self.hint is not None


def discover() -> list[Board]:
    """All serial ports that look like dev boards, known-VID boards first.

    On macOS both ``/dev/cu.*`` and ``/dev/tty.*`` exist for each port; only
    ``cu.*`` is usable for our purpose, and list_ports reports those.
    """
    boards = []
    for p in list_ports.comports():
        hint = VID_HINTS.get(p.vid) if p.vid else None
        boards.append(
            Board(
                device=p.device,
                description=p.description or "",
                vid=p.vid,
                pid=p.pid,
                serial_number=p.serial_number,
                hint=hint,
                location=p.location,
            )
        )
    boards.sort(key=lambda b: (not b.is_known, b.device))
    return boards


def resolve_port(spec: str | None = None) -> Board | None:
    """Resolve a port spec to a live Board.

    - None: best candidate (first known-VID board, else first USB serial port).
    - Exact device path, or substring match (``usbmodem`` matches the renumbered
      sibling after a replug).
    """
    boards = discover()
    if spec:
        for b in boards:
            if b.device == spec:
                return b
        matches = [b for b in boards if spec in b.device]
        matches.sort(key=lambda b: not b.is_known)
        if len(matches) > 1:
            devices = ", ".join(board.device for board in matches)
            raise AmbiguousPortError(f"port selector {spec!r} is ambiguous: {devices}")
        return matches[0] if matches else None
    known = [b for b in boards if b.is_known]
    if len(known) == 1:
        return known[0]
    if len(known) > 1:
        devices = ", ".join(board.device for board in known)
        raise AmbiguousPortError(f"multiple development boards found; specify --port: {devices}")
    usb = [b for b in boards if "usb" in b.device.lower()]
    if len(usb) == 1:
        return usb[0]
    if len(usb) > 1:
        devices = ", ".join(board.device for board in usb)
        raise AmbiguousPortError(f"multiple USB serial ports found; specify --port: {devices}")
    return boards[0] if len(boards) == 1 else None
