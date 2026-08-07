"""Bounded queries over session records.

Agents have token budgets; a 40KB log dump destroys the debug loop's
economics. Every query here is bounded (tail-limited) and structured, with
flood collapse for repeated lines. Consumers read files only — the serial
port stays with the capture daemon.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from .records import LogRecord

# Severity order for --level filters: showing W means E and W.
LEVEL_ORDER = {"E": 0, "W": 1, "I": 2, "D": 3, "V": 4}

DEFAULT_TAIL = 100


def iter_records(session: Path) -> Iterator[LogRecord]:
    path = session / "log.jsonl"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield LogRecord.from_json(line)
            except (json.JSONDecodeError, TypeError):
                continue  # a torn write at the tail is expected mid-capture


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
    buf: list[LogRecord] = []
    for r in records:
        buf.append(r)
        if len(buf) > n * 2:  # amortized trim instead of per-append
            del buf[: len(buf) - n]
    return buf[-n:]


def collapse_repeats(records: Iterable[LogRecord]) -> Iterator[LogRecord]:
    """Collapse consecutive identical (event, level, tag, msg) records into one
    record annotated with a repeat count — heartbeat/flood control."""
    prev: LogRecord | None = None
    count = 0
    for r in records:
        if (
            prev is not None
            and (r.event, r.level, r.tag, r.msg) == (prev.event, prev.level, prev.tag, prev.msg)
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


def latest_boot(session: Path) -> int:
    last = 0
    for r in iter_records(session):
        if r.boot > last:
            last = r.boot
    return last


def list_boots(session: Path) -> list[dict]:
    """One row per boot cycle: index, start time, line count, error count, crash count."""
    boots: dict[int, dict] = {}
    for r in iter_records(session):
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


def list_crashes(session: Path) -> list[dict]:
    crash_dir = session / "crashes"
    if not crash_dir.is_dir():
        return []
    out = []
    for path in sorted(crash_dir.glob("*.json")):
        try:
            out.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            continue
    return out


def format_record(r: LogRecord) -> str:
    """Compact single-line human/agent rendering."""
    parts = [r.ts[11:23]]  # HH:MM:SS.mmm
    parts.append(f"b{r.boot}")
    if r.event != "log":
        parts.append(f"<{r.event}>")
    if r.level:
        parts.append(r.level)
    if r.tag:
        parts.append(f"{r.tag}:")
    parts.append(r.msg)
    if r.extra and "repeat" in r.extra:
        parts.append(f"(×{r.extra['repeat']})")
    return " ".join(parts)
