"""Bounded queries over session records.

Agents have token budgets; a 40KB log dump destroys the debug loop's
economics. Every query here is bounded (tail-limited) and structured, with
flood collapse for repeated lines. Consumers read files only — the serial
port stays with the capture daemon.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections import deque
from collections.abc import Iterable, Iterator
from pathlib import Path

from .crash import MAX_CRASH_ARTIFACT_BYTES, MAX_CRASH_CONTENT_CHARS
from .parsers import strip_ansi
from .records import MAX_MESSAGE_CHARS, MAX_SRC_CHARS, MAX_TAG_CHARS, LogRecord
from .safety import bounded_timeout, compile_pattern, pattern_matches

# Severity order for --level filters: showing W means E and W.
LEVEL_ORDER = {"E": 0, "W": 1, "I": 2, "D": 3, "V": 4}

DEFAULT_TAIL = 100
MAX_TAIL = 500
# A schema-valid record can contain the maximum message, tag, and source plus
# JSON structure/escaping. Keep the reader boundary above the writer contract
# while still rejecting arbitrarily large or corrupt lines.
MAX_JSON_LINE_BYTES = 6 * (MAX_MESSAGE_CHARS + MAX_TAG_CHARS + MAX_SRC_CHARS) + 16 * 1024
FOLLOW_READ_BYTES = 256 * 1024
MAX_CRASH_FILE_BYTES = MAX_CRASH_ARTIFACT_BYTES
MAX_CRASH_RESULTS = 500
MAX_CRASH_SCAN_ENTRIES = 10_000
DEFAULT_CLI_SCAN_BYTES = 64 * 1024 * 1024


def configured_cli_scan_bytes() -> int:
    """Maximum bytes a single CLI log query scans from the newest tail."""
    raw = os.environ.get("HWLOG_QUERY_SCAN_BYTES")
    if raw is None:
        return DEFAULT_CLI_SCAN_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("HWLOG_QUERY_SCAN_BYTES must be an integer number of bytes") from exc
    if value < 0:
        raise ValueError("HWLOG_QUERY_SCAN_BYTES must be non-negative")
    return value


def _open_regular(path: Path):
    """Open a regular file without following symlinks or blocking on FIFOs."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        stream = os.fdopen(fd, "rb")
        fd = -1
        return stream
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def _read_json_regular(path: Path, maximum: int) -> object | None:
    stream = _open_regular(path)
    if stream is None:
        return None
    try:
        with stream:
            if os.fstat(stream.fileno()).st_size > maximum:
                return None
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            return None
        return json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None


def record_scan_is_truncated(session: Path, maximum: int) -> bool:
    """Whether a newest-tail scan omits older bytes from the session log."""
    stream = _open_regular(session / "log.jsonl")
    if stream is None:
        return False
    with stream:
        return os.fstat(stream.fileno()).st_size > max(0, maximum)


def _record_from_bytes(line: bytes) -> LogRecord | None:
    try:
        return LogRecord.from_json(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return None


def _validate_crash_report(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    crash_id = value.get("crash_id")
    if isinstance(crash_id, bool) or not isinstance(crash_id, int) or crash_id <= 0:
        return None
    if not isinstance(value.get("first_line"), str):
        return None
    for key in ("lines", "backtrace_addrs", "decoded_frames"):
        items = value.get(key)
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            return None
    remaining = MAX_CRASH_CONTENT_CHARS

    def take_text(text: str) -> str:
        nonlocal remaining
        result = text[:remaining]
        remaining -= len(result)
        return result

    def take_list(items: list[str]) -> list[str]:
        out = []
        for item in items:
            if remaining <= 0:
                break
            out.append(take_text(item))
        return out

    return {
        "crash_id": crash_id,
        "first_line": take_text(value["first_line"]),
        "lines": take_list(value["lines"]),
        "backtrace_addrs": take_list(value["backtrace_addrs"]),
        "decoded_frames": take_list(value["decoded_frames"]),
    }


def iter_records(session: Path, *, max_bytes: int | None = None) -> Iterator[LogRecord]:
    path = session / "log.jsonl"
    f = _open_regular(path)
    if f is None:
        return
    with f:
        end = os.fstat(f.fileno()).st_size
        if max_bytes is not None:
            max_bytes = max(0, max_bytes)
            start = max(0, end - max_bytes)
            if start:
                f.seek(start - 1)
                on_boundary = f.read(1) == b"\n"
                f.seek(start)
                if not on_boundary:
                    # The scan starts in an arbitrary record; discard through its newline.
                    while f.tell() < end and (
                        chunk := f.readline(min(MAX_JSON_LINE_BYTES + 1, end - f.tell()))
                    ):
                        if chunk.endswith(b"\n"):
                            break
        while f.tell() < end and (line := f.readline(min(MAX_JSON_LINE_BYTES + 1, end - f.tell()))):
            oversized = len(line) > MAX_JSON_LINE_BYTES
            if oversized and not line.endswith(b"\n"):
                while f.tell() < end and line and not line.endswith(b"\n"):
                    line = f.readline(min(MAX_JSON_LINE_BYTES + 1, end - f.tell()))
                continue
            line = line.strip()
            if not line or oversized:
                continue
            record = _record_from_bytes(line)
            if record is not None:
                yield record


def filter_records(
    records: Iterable[LogRecord],
    *,
    boot: int | None = None,
    level: str | None = None,
    grep: str | None = None,
    tag: str | None = None,
    since: str | None = None,
    event: str | None = None,
) -> Iterator[LogRecord]:
    """boot: exact index, or -1 for the latest boot (resolved by caller)."""
    max_level = LEVEL_ORDER.get(level.upper(), None) if level else None
    grep_lower = grep.lower() if grep else None
    for r in records:
        if boot is not None and r.boot != boot:
            continue
        if event is not None and r.event != event:
            continue
        if max_level is not None:
            if r.level is None or LEVEL_ORDER.get(r.level, 99) > max_level:
                continue
        if tag is not None and r.tag != tag:
            continue
        if since is not None and r.ts < since:
            continue
        if grep_lower is not None and grep_lower not in r.msg.lower():
            continue
        yield r


def tail(records: Iterable[LogRecord], n: int = DEFAULT_TAIL) -> list[LogRecord]:
    n = max(0, min(n, MAX_TAIL))
    if n == 0:
        return []
    return list(deque(records, maxlen=n))


def collapse_repeats(records: Iterable[LogRecord]) -> Iterator[LogRecord]:
    """Collapse consecutive identical (event, level, tag, msg) records into one
    record annotated with a repeat count — heartbeat/flood control."""
    prev: LogRecord | None = None
    count = 0
    for r in records:
        if prev is not None and (r.event, r.level, r.tag, r.msg) == (
            prev.event,
            prev.level,
            prev.tag,
            prev.msg,
        ):
            count += 1
            prev = r  # keep the newest timestamp
            continue
        if prev is not None:
            yield _annotate(prev, count)
        prev, count = r, 1
    if prev is not None:
        yield _annotate(prev, count)


def _annotate(record: LogRecord, count: int) -> LogRecord:
    if count > 1:
        record.extra = {**(record.extra or {}), "repeat": count}
    return record


def latest_boot(session: Path, *, max_bytes: int | None = None) -> int:
    last = 0
    for r in iter_records(session, max_bytes=max_bytes):
        if r.boot > last:
            last = r.boot
    return last


def list_boots(session: Path, *, max_bytes: int | None = None) -> list[dict]:
    """One row per boot cycle: index, start time, line count, error count, crash count."""
    boots: dict[int, dict] = {}
    for r in iter_records(session, max_bytes=max_bytes):
        b = boots.setdefault(
            r.boot, {"boot": r.boot, "started": r.ts, "lines": 0, "errors": 0, "crashes": 0}
        )
        b["lines"] += 1
        b["ended"] = r.ts
        if r.level == "E":
            b["errors"] += 1
        if r.event == "crash":
            b["crashes"] += 1
    return [boots[k] for k in sorted(boots)]


def list_crashes(session: Path, *, limit: int | None = MAX_CRASH_RESULTS) -> list[dict]:
    crash_dir = session / "crashes"
    if crash_dir.is_symlink() or not crash_dir.is_dir():
        return []
    requested = MAX_CRASH_RESULTS if limit is None else max(0, min(limit, MAX_CRASH_RESULTS))
    paths = _expected_crash_paths(session, requested)
    out = []
    for crash_id, path in paths:
        report = _validate_crash_report(_read_json_regular(path, MAX_CRASH_FILE_BYTES))
        if report is not None and report["crash_id"] == crash_id:
            out.append(report)
    return out


def _expected_crash_paths(session: Path, limit: int) -> list[tuple[int, Path]]:
    """Use metadata for O(limit) history access, with a bounded legacy fallback."""
    if limit <= 0:
        return []
    crash_dir = session / "crashes"
    try:
        from .session import read_meta

        latest = read_meta(session).crashes
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        latest = 0
    if latest > 0:
        # write_crash publishes the artifact before metadata. If the process dies
        # between those two atomic writes, exactly one consecutive artifact can
        # be ahead of the persisted count; recover it without enumerating a tree.
        next_path = crash_dir / f"{latest + 1:03d}.json"
        try:
            next_info = next_path.lstat()
        except OSError:
            next_info = None
        effective_latest = (
            latest + 1 if next_info is not None and stat.S_ISREG(next_info.st_mode) else latest
        )
        first = max(1, effective_latest - limit + 1)
        return [
            (crash_id, crash_dir / f"{crash_id:03d}.json")
            for crash_id in range(first, effective_latest + 1)
        ]
    return _crash_paths_bounded(crash_dir, limit)


def _crash_paths_bounded(crash_dir: Path, limit: int) -> list[tuple[int, Path]]:
    """Bound both enumeration work and retained paths for legacy/corrupt metadata."""
    paths = []
    try:
        entries = os.scandir(crash_dir)
    except OSError:
        return []
    with entries:
        for index, entry in enumerate(entries):
            if index >= MAX_CRASH_SCAN_ENTRIES:
                break
            if not entry.name.endswith(".json"):
                continue
            try:
                crash_id = int(entry.name[:-5])
            except ValueError:
                continue
            if crash_id > 0:
                paths.append((crash_id, Path(entry.path)))
    paths.sort(key=lambda item: item[0])
    return paths[-limit:]


def get_crash(session: Path, crash_id: int | None = None) -> dict | None:
    """Read one bounded crash artifact without loading the entire history."""
    crash_dir = session / "crashes"
    if crash_dir.is_symlink() or not crash_dir.is_dir():
        return None
    if crash_id is None:
        candidates = reversed(_expected_crash_paths(session, MAX_CRASH_RESULTS))
    else:
        if isinstance(crash_id, bool) or not isinstance(crash_id, int) or crash_id <= 0:
            return None
        candidates = iter(((crash_id, crash_dir / f"{crash_id:03d}.json"),))
    for expected_id, path in candidates:
        report = _validate_crash_report(_read_json_regular(path, MAX_CRASH_FILE_BYTES))
        if report is not None and report["crash_id"] == expected_id:
            return report
    return None


def format_record(r: LogRecord) -> str:
    """Compact single-line human/agent rendering."""
    parts = [strip_ansi(r.ts[11:23])]  # HH:MM:SS.mmm
    parts.append(f"b{r.boot}")
    if r.event != "log":
        parts.append(f"<{strip_ansi(r.event)}>")
    if r.level:
        parts.append(strip_ansi(r.level))
    if r.tag:
        parts.append(f"{strip_ansi(r.tag)}:")
    parts.append(strip_ansi(r.msg))
    if r.extra and "repeat" in r.extra:
        parts.append(f"(×{strip_ansi(str(r.extra['repeat']))})")
    return " ".join(parts)


class LogFollower:
    """Incrementally read complete JSONL records without losing a torn tail."""

    def __init__(
        self,
        path: Path,
        *,
        from_start: bool = False,
        start_offset: int | None = None,
    ) -> None:
        self.path = path
        initial = _open_regular(path)
        if initial is None:
            self.offset = 0
        else:
            with initial:
                size = os.fstat(initial.fileno()).st_size
                if from_start:
                    self.offset = 0
                elif start_offset is not None:
                    self.offset = max(0, min(start_offset, size))
                else:
                    self.offset = size
        self._pending = b""
        self._discarding_oversized = False

    def read(self, max_bytes: int = FOLLOW_READ_BYTES) -> list[LogRecord]:
        f = _open_regular(self.path)
        if f is None:
            return []
        try:
            with f:
                size = os.fstat(f.fileno()).st_size
                if size < self.offset:
                    self.offset = 0
                    self._pending = b""
                    self._discarding_oversized = False
                f.seek(self.offset)
                chunk = f.read(max(1, min(max_bytes, FOLLOW_READ_BYTES)))
        except OSError:
            return []
        if not chunk:
            return []
        self.offset += len(chunk)

        if self._discarding_oversized:
            newline = chunk.find(b"\n")
            if newline < 0:
                return []
            chunk = chunk[newline + 1 :]
            self._discarding_oversized = False

        data = self._pending + chunk
        lines = data.split(b"\n")
        self._pending = lines.pop()
        if len(self._pending) > MAX_JSON_LINE_BYTES:
            self._pending = b""
            self._discarding_oversized = True

        records = []
        for line in lines:
            if not line or len(line) > MAX_JSON_LINE_BYTES:
                continue
            record = _record_from_bytes(line)
            if record is not None:
                records.append(record)
        return records


def wait_for_record(
    session: Path,
    pattern: str,
    timeout: float,
    *,
    from_start: bool = False,
) -> LogRecord | None:
    """Wait for a safe regex in complete, newly appended records."""
    compiled = compile_pattern(pattern)
    timeout = bounded_timeout(timeout)
    start_offset = None
    if not from_start:
        try:
            from .session import read_meta

            start_offset = read_meta(session).wait_from_offset
        except (OSError, UnicodeDecodeError, RecursionError, TypeError, ValueError):
            pass
    follower = LogFollower(
        session / "log.jsonl",
        from_start=from_start,
        start_offset=start_offset,
    )
    deadline = time.monotonic() + timeout
    while True:
        records = follower.read()
        for record in records:
            if pattern_matches(compiled, record.msg):
                return record
            if time.monotonic() >= deadline:
                return None
        if time.monotonic() >= deadline:
            return None
        # Drain a busy file without adding avoidable latency; otherwise poll.
        if len(records) == 0:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
