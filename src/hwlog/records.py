"""Log record schema and JSONL (de)serialization.

One record per line of device output (or per synthetic event). The schema is
the contract between capture (writer) and every consumer (CLI queries, MCP
tools, humans with jq), so it stays flat, small, and stable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

MAX_COUNTER = (1 << 63) - 1
MAX_MESSAGE_CHARS = 64 * 1024
MAX_TAG_CHARS = 1024
MAX_SRC_CHARS = 1024


class Event(StrEnum):
    LOG = "log"  # a parsed or raw device log line
    BOOT = "boot"  # a device reset/boot was detected
    CRASH = "crash"  # an assembled crash report (see crashes/ for full artifact)
    STATUS = "status"  # capture-side lifecycle: port opened/lost/reopened
    SENT = "sent"  # bytes the host sent to the device


class Level(StrEnum):
    ERROR = "E"
    WARN = "W"
    INFO = "I"
    DEBUG = "D"
    VERBOSE = "V"


@dataclass
class LogRecord:
    ts: str  # host ISO-8601 UTC timestamp
    seq: int  # monotonic per session
    boot: int  # boot cycle index within session (0-based)
    event: str = Event.LOG
    level: str | None = None  # E/W/I/D/V when parsed, None for freeform
    tag: str | None = None  # subsystem tag when parsed
    msg: str = ""  # cleaned message (ANSI stripped)
    dev_ts: int | None = None  # device-side ms timestamp when parsed
    src: str | None = None  # file:line for formats that carry it (Arduino core)
    extra: dict | None = None  # event-specific payload (crash id, port state, ...)

    def to_json(self, *, ensure_ascii: bool = False) -> str:
        d = {k: v for k, v in asdict(self).items() if v is not None and v != ""}
        # msg is meaningful even when empty for non-log events; keep key stable for logs
        if self.event == Event.LOG and "msg" not in d:
            d["msg"] = ""
        return json.dumps(d, ensure_ascii=ensure_ascii)

    @classmethod
    def from_json(cls, line: str) -> LogRecord:
        try:
            d = json.loads(line)
        except RecursionError as exc:
            raise ValueError("log record JSON is too deeply nested") from exc
        if not isinstance(d, dict):
            raise ValueError("log record must be a JSON object")
        known = {f_.name for f_ in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        record = cls(**{k: v for k, v in d.items() if k in known})
        if not isinstance(record.ts, str) or not record.ts or len(record.ts) > 64:
            raise ValueError("record ts must be a non-empty string")
        if (
            isinstance(record.seq, bool)
            or not isinstance(record.seq, int)
            or not 0 <= record.seq <= MAX_COUNTER
        ):
            raise ValueError("record seq must be a non-negative integer")
        if (
            isinstance(record.boot, bool)
            or not isinstance(record.boot, int)
            or not 0 <= record.boot <= MAX_COUNTER
        ):
            raise ValueError("record boot must be a non-negative integer")
        if not isinstance(record.event, str) or not isinstance(record.msg, str):
            raise ValueError("record event and msg must be strings")
        if len(record.event) > 32 or len(record.msg) > MAX_MESSAGE_CHARS:
            raise ValueError("record event or msg is too long")
        if record.level is not None and (
            not isinstance(record.level, str)
            or record.level not in {level.value for level in Level}
        ):
            raise ValueError("record level is invalid")
        for name, maximum in (("tag", MAX_TAG_CHARS), ("src", MAX_SRC_CHARS)):
            value = getattr(record, name)
            if value is not None and (not isinstance(value, str) or len(value) > maximum):
                raise ValueError(f"record {name} must be a string or null")
        if record.dev_ts is not None and (
            isinstance(record.dev_ts, bool) or not isinstance(record.dev_ts, int)
        ):
            raise ValueError("record dev_ts must be an integer or null")
        if record.dev_ts is not None and abs(record.dev_ts) > MAX_COUNTER:
            raise ValueError("record dev_ts is out of range")
        if record.extra is not None and not isinstance(record.extra, dict):
            raise ValueError("record extra must be an object or null")
        return record


@dataclass
class SessionMeta:
    session_id: str
    port: str
    baud: int
    started_at: str
    ended_at: str | None = None
    usb_vid: int | None = None
    usb_pid: int | None = None
    usb_serial: str | None = None
    usb_location: str | None = None
    board_hint: str | None = None  # e.g. "ESP32-S3 (native USB)" from VID/PID table
    elf_paths: list[str] = field(default_factory=list)  # archived ELFs, newest last
    elf_pending: bool = False  # successful flash awaits a matching fresh ELF
    firmware_generation: int = 0
    elf_generation: int = 0
    wait_from_offset: int | None = None  # post-flash wait boundary in log.jsonl
    storage_capped: bool = False
    storage_cap_reason: str | None = None
    storage_limit_bytes: int | None = None
    dropped_raw_bytes: int = 0
    dropped_records: int = 0
    dropped_crashes: int = 0
    boots: int = 0
    crashes: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> SessionMeta:
        try:
            d = json.loads(text)
        except RecursionError as exc:
            raise ValueError("session metadata JSON is too deeply nested") from exc
        if not isinstance(d, dict):
            raise ValueError("session metadata must be a JSON object")
        known = {f_.name for f_ in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        meta = cls(**{k: v for k, v in d.items() if k in known})
        if not all(
            isinstance(value, str) for value in (meta.session_id, meta.port, meta.started_at)
        ):
            raise ValueError("session identifiers and timestamps must be strings")
        if (
            len(meta.session_id) > 256
            or len(meta.port) > 4096
            or not 1 <= len(meta.started_at) <= 64
        ):
            raise ValueError("session identifier, port, or timestamp is too long")
        if (
            isinstance(meta.baud, bool)
            or not isinstance(meta.baud, int)
            or not 0 < meta.baud <= 100_000_000
        ):
            raise ValueError("session baud must be a positive integer")
        for name, maximum in (
            ("ended_at", 64),
            ("usb_serial", 1024),
            ("usb_location", 1024),
            ("board_hint", 4096),
        ):
            value = getattr(meta, name)
            if value is not None and (not isinstance(value, str) or len(value) > maximum):
                raise ValueError(f"session {name} must be a string or null")
        for name in ("usb_vid", "usb_pid"):
            value = getattr(meta, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFF
            ):
                raise ValueError(f"session {name} must be an integer or null")
        if (
            not isinstance(meta.elf_paths, list)
            or len(meta.elf_paths) > 256
            or not all(isinstance(path, str) and len(path) <= 4096 for path in meta.elf_paths)
        ):
            raise ValueError("session elf_paths must be a list of strings")
        if not isinstance(meta.elf_pending, bool):
            raise ValueError("session elf_pending must be a boolean")
        if not isinstance(meta.storage_capped, bool):
            raise ValueError("session storage_capped must be a boolean")
        if meta.storage_cap_reason is not None and (
            not isinstance(meta.storage_cap_reason, str) or len(meta.storage_cap_reason) > 64
        ):
            raise ValueError("session storage_cap_reason must be a short string or null")
        if meta.storage_limit_bytes is not None and (
            isinstance(meta.storage_limit_bytes, bool)
            or not isinstance(meta.storage_limit_bytes, int)
            or not 0 <= meta.storage_limit_bytes <= MAX_COUNTER
        ):
            raise ValueError("session storage_limit_bytes must be a non-negative integer or null")
        for name in (
            "firmware_generation",
            "elf_generation",
            "dropped_raw_bytes",
            "dropped_records",
            "dropped_crashes",
            "boots",
            "crashes",
        ):
            value = getattr(meta, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= MAX_COUNTER
            ):
                raise ValueError(f"session {name} must be a non-negative integer")
        if meta.elf_generation > meta.firmware_generation:
            raise ValueError("session ELF generation cannot exceed firmware generation")
        if meta.wait_from_offset is not None and (
            isinstance(meta.wait_from_offset, bool)
            or not isinstance(meta.wait_from_offset, int)
            or not 0 <= meta.wait_from_offset <= MAX_COUNTER
        ):
            raise ValueError("session wait offset must be a non-negative integer or null")
        return meta


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
