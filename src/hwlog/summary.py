"""Compact, read-only evidence for the agent's observe/revise loop."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict
from pathlib import Path

from . import query
from .parsers import strip_ansi
from .records import Event, LogRecord
from .session import read_meta

MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_FAULTS = 8
MAX_TAGS = 20
MAX_TEXT_CHARS = 256
MAX_TEXT_JSON_BYTES = 512
MAX_OUTPUT_BYTES = 32 * 1024


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = strip_ansi(value)
    if len(cleaned) <= MAX_TEXT_CHARS and len(json.dumps(cleaned)) <= MAX_TEXT_JSON_BYTES:
        return cleaned
    low, high = 0, min(len(cleaned), MAX_TEXT_CHARS - 1)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(cleaned[:middle] + "…")) <= MAX_TEXT_JSON_BYTES:
            low = middle
        else:
            high = middle - 1
    return cleaned[:low] + "…"


def _record(record: LogRecord) -> dict:
    # Explicit projection: extra can contain arbitrary device-supplied objects.
    return {
        "seq": record.seq,
        "ts": _text(record.ts),
        "boot": record.boot,
        "event": _text(record.event),
        "level": record.level,
        "tag": _text(record.tag),
        "msg": _text(record.msg),
        "text_truncated": any(
            _text(value) != strip_ansi(value)
            for value in (record.tag, record.msg)
            if value is not None
        ),
    }


def summarize(session: Path, *, scan_bytes: int = MAX_SCAN_BYTES) -> dict:
    """Summarize valid records in a bounded newest-tail snapshot.

    Counts cover the scanned window only. Metadata counters are reported
    separately; neither an empty log nor an error-free window proves health.
    """
    if isinstance(scan_bytes, bool) or not isinstance(scan_bytes, int) or scan_bytes < 0:
        raise ValueError("scan bytes must be a non-negative integer")
    budget = min(scan_bytes, MAX_SCAN_BYTES)
    meta = read_meta(session)
    stats = query.ScanStats()
    counts = {"records": 0, "errors": 0, "warnings": 0, "boot_events": 0, "crashes": 0}
    latest_boot = None
    last_record = None
    faults: deque[dict] = deque(maxlen=MAX_FAULTS)
    fault_count = 0
    tags: dict[str, int] = {}
    untracked_tag_records = 0
    for record in query.iter_records(session, max_bytes=budget, stats=stats):
        counts["records"] += 1
        counts["errors"] += record.level == "E"
        counts["warnings"] += record.level == "W"
        counts["boot_events"] += record.event == Event.BOOT
        counts["crashes"] += record.event == Event.CRASH
        if latest_boot is None or record.boot > latest_boot:
            latest_boot = record.boot
        last_record = record
        if record.level in {"E", "W"} or record.event == Event.CRASH:
            faults.append(_record(record))
            fault_count += 1
        if record.tag is not None:
            if record.tag in tags or len(tags) < MAX_TAGS:
                tags[record.tag] = tags.get(record.tag, 0) + 1
            else:
                untracked_tag_records += 1
    result = {
        "schema_version": 1,
        "trust": "untrusted_device_output",
        "session_id": _text(meta.session_id),
        "device": {
            "port": _text(meta.port),
            "board_hint": _text(meta.board_hint),
            "usb_vid": meta.usb_vid,
            "usb_pid": meta.usb_pid,
            "usb_serial": _text(meta.usb_serial),
        },
        "started_at": _text(meta.started_at),
        "ended_at": _text(meta.ended_at),
        "firmware": {
            "generation": meta.firmware_generation,
            "elf_generation": meta.elf_generation,
            "elf_pending": meta.elf_pending,
        },
        "storage": {
            "capped": meta.storage_capped,
            "cap_reason": _text(meta.storage_cap_reason),
            "dropped_raw_bytes": meta.dropped_raw_bytes,
            "dropped_records": meta.dropped_records,
            "dropped_crashes": meta.dropped_crashes,
        },
        "scan": {**asdict(stats), "budget_bytes": budget},
        "counts": counts,
        "latest_boot": latest_boot,
        "last_record": _record(last_record) if last_record is not None else None,
        "recent_faults": list(faults),
        "omitted_faults": max(0, fault_count - MAX_FAULTS),
        "tags": [
            {"tag": _text(tag), "records": count, "text_truncated": _text(tag) != strip_ansi(tag)}
            for tag, count in sorted(tags.items(), key=lambda item: (-item[1], item[0]))
        ],
        "untracked_tag_records": untracked_tag_records,
    }
    # Keep this check at the public boundary so future fields cannot silently
    # invalidate the budget guaranteed by the fixed lists and bounded strings.
    if len(json.dumps(result, ensure_ascii=True)) > MAX_OUTPUT_BYTES:
        raise ValueError("session summary exceeded the response budget")
    return result
