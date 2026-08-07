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

import contextlib
import os
import re
import selectors
import shutil
import signal
import subprocess
import time
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
    r"Task watchdog got triggered|Watchdog timeout|panic'ed|Fatal exception|"
    r"Exception was unhandled|Stack canary watchpoint triggered|"
    r"A stack overflow in task .* has been detected",
    re.IGNORECASE,
)

BACKTRACE_RE = re.compile(r"Backtrace:\s*((?:0x[0-9a-fA-F]+:0x[0-9a-fA-F]+\s*)+)")
ADDR_PAIR_RE = re.compile(r"(0x[0-9a-fA-F]+):0x[0-9a-fA-F]+")

# End-of-artifact markers: the chip reboots after a panic.
CRASH_END_RE = re.compile(r"^Rebooting\.\.\.|^ELF file SHA256:|^rst:0x")

MAX_CRASH_LINES = 120  # register dumps + backtrace fit comfortably; bound the artifact
MAX_CRASH_CHARS = 64 * 1024
MAX_CRASH_ARTIFACT_BYTES = 1024 * 1024
MAX_CRASH_CONTENT_CHARS = 160 * 1024
MAX_BACKTRACE_ADDRS = 2048
MAX_DECODED_FRAMES = 512
MAX_DECODED_CHARS = 64 * 1024
MAX_DECODED_FRAME_CHARS = 2048
ADDR2LINE_TIMEOUT_S = 10.0
MAX_ADDR2LINE_STDOUT_BYTES = 256 * 1024
MAX_ADDR2LINE_STDERR_BYTES = 64 * 1024
ADDR2LINE_READ_CHUNK_BYTES = 16 * 1024
ADDR2LINE_POLL_S = 0.05
ADDR2LINE_REAP_GRACE_S = 0.25


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
            self._active = CrashReport(crash_id=self._count + 1, first_line=line, lines=[line])
            self._count += 1
            return finished

        report.lines.append(line)
        if (
            CRASH_END_RE.search(line)
            or len(report.lines) >= MAX_CRASH_LINES
            or sum(map(len, report.lines)) >= MAX_CRASH_CHARS
        ):
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


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the isolated addr2line process group without trusting its descendants."""
    with contextlib.suppress(OSError):
        os.killpg(process.pid, signal.SIGKILL)
    # If session creation failed in an unusual platform state, still kill the
    # direct child. Popen.kill is harmless when it has already exited.
    with contextlib.suppress(OSError):
        process.kill()


def _bounded_addr2line_stdout(command: list[str]) -> bytes | None:
    """Run addr2line with bounded pipes and a hard execution deadline.

    Pipes are consumed with nonblocking POSIX selectors instead of
    ``communicate()`` so a descendant that inherits stdout/stderr cannot keep the
    caller blocked after the direct child exits.
    """
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        return None

    assert process.stdout is not None
    assert process.stderr is not None
    streams = (process.stdout, process.stderr)
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {
        "stdout": MAX_ADDR2LINE_STDOUT_BYTES,
        "stderr": MAX_ADDR2LINE_STDERR_BYTES,
    }
    deadline = time.monotonic() + ADDR2LINE_TIMEOUT_S
    failed = False

    try:
        for name, stream in zip(("stdout", "stderr"), streams, strict=True):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)

        while True:
            now = time.monotonic()
            if now >= deadline:
                failed = True
                break

            if process.poll() is not None:
                # The direct child is done. Kill any descendants before draining
                # bytes already buffered in the pipes; they must not hold the fds
                # open or continue producing unbounded output.
                _kill_process_group(process)
                while time.monotonic() < deadline:
                    ready = selector.select(0)
                    if not ready:
                        break
                    if _consume_addr2line_events(selector, ready, buffers, limits):
                        failed = True
                        break
                break

            timeout = min(ADDR2LINE_POLL_S, max(0.0, deadline - now))
            ready = selector.select(timeout) if selector.get_map() else ()
            if not ready:
                if not selector.get_map():
                    time.sleep(timeout)
                continue
            if _consume_addr2line_events(selector, ready, buffers, limits):
                failed = True
                break
    except OSError:
        failed = True
    finally:
        # Always clear the whole isolated group. This also handles exceptions and
        # successful tools that leave a pipe-holding descendant behind.
        _kill_process_group(process)
        selector.close()
        for stream in streams:
            with contextlib.suppress(OSError):
                stream.close()
        if process.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=ADDR2LINE_REAP_GRACE_S)

    return None if failed else bytes(buffers["stdout"])


def _consume_addr2line_events(
    selector: selectors.BaseSelector,
    ready,
    buffers: dict[str, bytearray],
    limits: dict[str, int],
) -> bool:
    """Consume one nonblocking chunk per ready stream; return True on overflow."""
    for key, _mask in ready:
        name = key.data
        buffer = buffers[name]
        remaining = limits[name] - len(buffer)
        try:
            chunk = os.read(
                key.fileobj.fileno(),
                min(ADDR2LINE_READ_CHUNK_BYTES, remaining + 1),
            )
        except BlockingIOError:
            continue
        if not chunk:
            with contextlib.suppress(Exception):
                selector.unregister(key.fileobj)
            continue
        if len(chunk) > remaining:
            buffer.extend(chunk[:remaining])
            return True
        buffer.extend(chunk)
    return False


def decode_backtrace(addrs: list[str], elf_path: str, addr2line: str | None = None) -> list[str]:
    """Resolve program-counter addresses to ``function at file:line`` frames."""
    tool = addr2line or find_addr2line()
    if not tool or not addrs:
        return []
    stdout = _bounded_addr2line_stdout(
        [tool, "-pfiaC", "-e", elf_path, *addrs[:MAX_BACKTRACE_ADDRS]]
    )
    if stdout is None:
        return []
    frames = []
    total = 0
    for line in stdout.decode("utf-8", "replace").splitlines():
        frame = line.strip()[:MAX_DECODED_FRAME_CHARS]
        if not frame:
            continue
        remaining = MAX_DECODED_CHARS - total
        if remaining <= 0 or len(frames) >= MAX_DECODED_FRAMES:
            break
        frame = frame[:remaining]
        frames.append(frame)
        total += len(frame)
    # addr2line prints "0x... : ?? at ??:0" for unknown frames; keep them, they show depth.
    return frames
