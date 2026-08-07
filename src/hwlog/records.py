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


class Event(StrEnum):
    LOG = "log"          # a parsed or raw device log line
    BOOT = "boot"        # a device reset/boot was detected
    CRASH = "crash"      # an assembled crash report (see crashes/ for full artifact)
    STATUS = "status"    # capture-side lifecycle: port opened/lost/reopened
    SENT = "sent"        # bytes the host sent to the device


class Level(StrEnum):
    ERROR = "E"
    WARN = "W"
    INFO = "I"
    DEBUG = "D"
    VERBOSE = "V"


@dataclass
class LogRecord:
    ts: str                        # host ISO-8601 UTC timestamp
    seq: int                       # monotonic per session
    boot: int                      # boot cycle index within session (0-based)
    event: str = Event.LOG
    level: str | None = None       # E/W/I/D/V when parsed, None for freeform
    tag: str | None = None         # subsystem tag when parsed
    msg: str = ""                  # cleaned message (ANSI stripped)
    dev_ts: int | None = None      # device-side ms timestamp when parsed
    src: str | None = None         # file:line for formats that carry it (Arduino core)
    extra: dict | None = None      # event-specific payload (crash id, port state, ...)

    def to_json(self) -> str:
        d = {k: v for k, v in asdict(self).items() if v is not None and v != ""}
        # msg is meaningful even when empty for non-log events; keep key stable for logs
        if self.event == Event.LOG and "msg" not in d:
            d["msg"] = ""
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> LogRecord:
        d = json.loads(line)
        known = {f_.name for f_ in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


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
    board_hint: str | None = None      # e.g. "ESP32-S3 (native USB)" from VID/PID table
    elf_paths: list[str] = field(default_factory=list)  # archived ELFs, newest last
    boots: int = 0
    crashes: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> SessionMeta:
        d = json.loads(text)
        known = {f_.name for f_ in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
