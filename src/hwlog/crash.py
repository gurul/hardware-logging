"""Crash detection, multi-line crash assembly, and backtrace symbolization.

A decoded backtrace is worth ten thousand raw log lines. This module:

1. Flags crash-signature lines (panics, watchdogs, heap corruption, brownout).
   Signature list seeded from real ESP32 field incidents.
2. Assembles the full multi-line crash artifact (register dump + backtrace)
   into one structured report instead of leaving it smeared across the stream.
3. Symbolizes backtrace addresses with ``addr2line`` when a matching ELF was
   archived at flash time.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field

# Battle-tested signature set (each pattern earned its place in a real incident).
CRASH_RE = re.compile(
    r"Guru Meditation|Backtrace:|abort\(\) was called|assert failed|"
    r"wdt|watchdog|StoreProhibited|LoadProhibited|InstrFetchProhibited|"
    r"IllegalInstruction|IntegerDivideByZero|Unhandled debug exception|"
    r"CORRUPT HEAP|heap corrupt|Stack smashing|stack overflow|"
    r"Brownout detector|Exception was unhandled|panic'ed|Panic|Fatal exception",
    re.IGNORECASE,
)

# The trigger lines that *start* a crash artifact (vs. lines that merely look scary).
CRASH_START_RE = re.compile(
    r"Guru Meditation Error|abort\(\) was called|assert failed|"
    r"Unhandled debug exception|CORRUPT HEAP|Stack smashing|Brownout detector|"
    r"Task watchdog got triggered|E \(\d+\) task_wdt",
)

BACKTRACE_RE = re.compile(r"Backtrace:\s*((?:0x[0-9a-fA-F]+:0x[0-9a-fA-F]+\s*)+)")
ADDR_PAIR_RE = re.compile(r"(0x[0-9a-fA-F]+):0x[0-9a-fA-F]+")

# End-of-artifact markers: the chip reboots after a panic.
CRASH_END_RE = re.compile(r"^Rebooting\.\.\.|^ELF file SHA256:|^rst:0x")

MAX_CRASH_LINES = 120  # register dumps + backtrace fit comfortably; bound the artifact


def is_crash_line(line: str) -> bool:
    return bool(CRASH_RE.search(line))


@dataclass
class CrashReport:
    crash_id: int
    first_line: str
    lines: list[str] = field(default_factory=list)
    backtrace_addrs: list[str] = field(default_factory=list)
    decoded_frames: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = self.first_line.strip()
        bt = f" backtrace[{len(self.backtrace_addrs)}]" if self.backtrace_addrs else ""
        dec = " decoded" if self.decoded_frames else ""
        return f"{head}{bt}{dec}"


class CrashAssembler:
    """Feed lines in stream order; emits a CrashReport when an artifact completes.

    Usage: ``report = assembler.feed(line)`` — returns None until a full crash
    artifact has been collected (or the line budget is hit).
    """

    def __init__(self) -> None:
        self._active: CrashReport | None = None
        self._count = 0

    @property
    def in_crash(self) -> bool:
        return self._active is not None

    def feed(self, line: str) -> CrashReport | None:
        if self._active is None:
            if CRASH_START_RE.search(line):
                self._count += 1
                self._active = CrashReport(crash_id=self._count, first_line=line, lines=[line])
            return None

        report = self._active
        # A new crash start while assembling means the previous artifact ended abruptly.
        if CRASH_START_RE.search(line) and len(report.lines) > 1:
            finished = self._finish()
            self._active = CrashReport(
                crash_id=self._count + 1, first_line=line, lines=[line]
            )
            self._count += 1
            return finished

        report.lines.append(line)
        if CRASH_END_RE.search(line) or len(report.lines) >= MAX_CRASH_LINES:
            return self._finish()
        return None

    def flush(self) -> CrashReport | None:
        """Force-complete an in-flight artifact (e.g. on port loss)."""
        return self._finish() if self._active else None

    def _finish(self) -> CrashReport:
        report = self._active
        assert report is not None
        self._active = None
        for line in report.lines:
            m = BACKTRACE_RE.search(line)
            if m:
                report.backtrace_addrs = ADDR_PAIR_RE.findall(m.group(0))
        return report


# --- Symbolization ---------------------------------------------------------

# Try target-specific toolchains first, then generic binutils.
ADDR2LINE_CANDIDATES = (
    "xtensa-esp32s3-elf-addr2line",
    "xtensa-esp32-elf-addr2line",
    "xtensa-esp-elf-addr2line",
    "riscv32-esp-elf-addr2line",
    "addr2line",
)


def find_addr2line() -> str | None:
    for name in ADDR2LINE_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def decode_backtrace(addrs: list[str], elf_path: str, addr2line: str | None = None) -> list[str]:
    """Resolve program-counter addresses to ``function at file:line`` frames."""
    tool = addr2line or find_addr2line()
    if not tool or not addrs:
        return []
    try:
        out = subprocess.run(
            [tool, "-pfiaC", "-e", elf_path, *addrs],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    frames = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    # addr2line prints "0x... : ?? at ??:0" for unknown frames; keep them, they show depth.
    return frames
