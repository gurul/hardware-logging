"""MCP server: hwlog as native agent tools.

Every tool is bounded and structured — no tool can dump an unbounded log
stream into the model's context. Reads come from session files; only ``send``
touches the daemon (which owns the port).
"""

from __future__ import annotations

import json
import re
import time

from fastmcp import FastMCP

from . import daemon, query
from . import ports as ports_mod
from .session import list_sessions, read_meta, resolve_session

mcp = FastMCP(
    "hardware-logging",
    instructions=(
        "Structured serial logs from embedded boards (ESP32 etc.). Capture runs as a "
        "separate daemon (`hwlog start`); these tools query recorded sessions. "
        "Typical loop: flash firmware with `hwlog flash -- <cmd>` (shell), then "
        "wait_for_pattern to verify behavior, then query_logs/get_crash to debug. "
        "Prefer boot=-1 (latest boot) and small tails; escalate to bigger windows "
        "only when needed."
    ),
)

MAX_TAIL = 500  # hard ceiling regardless of what the model asks for


def _session(selector: str | None = None):
    s = resolve_session(selector)
    if s is None:
        raise ValueError("no capture session found — run `hwlog start` in a shell first")
    return s


@mcp.tool
def list_serial_ports() -> list[dict]:
    """List serial ports with board identification (likely dev boards first)."""
    return [b.__dict__ for b in ports_mod.discover()]


@mcp.tool
def capture_status() -> dict:
    """Status of the capture daemon: running, connected, current session, counts."""
    daemons = daemon.list_daemons()
    if not daemons:
        return {"running": False, "hint": "start capture with `hwlog start` in a shell"}
    state = daemons[0]
    try:
        resp = daemon.send_cmd(state, {"cmd": "status"})
    except OSError as e:
        resp = {"ok": False, "error": str(e)}
    return {"running": True, **state, **resp}


@mcp.tool
def query_logs(
    tail: int = 50,
    boot: int | None = None,
    level: str | None = None,
    grep: str | None = None,
    tag: str | None = None,
    session: str | None = None,
    collapse_repeats: bool = True,
) -> list[str]:
    """Query captured device logs, newest last. Bounded: max 500 lines.

    boot: exact boot index, or -1 for the latest boot cycle (recommended).
    level: minimum severity — E (errors only), W, I, D, V.
    grep: case-insensitive substring filter on the message.
    collapse_repeats: fold repeated identical lines into one entry with a count.
    """
    s = _session(session)
    b = query.latest_boot(s) if boot == -1 else boot
    recs = query.filter_records(
        query.iter_records(s), boot=b, level=level, grep=grep, tag=tag
    )
    if collapse_repeats:
        recs = query.collapse_repeats(recs)
    return [query.format_record(r) for r in query.tail(recs, min(tail, MAX_TAIL))]


@mcp.tool
def list_boots(session: str | None = None) -> list[dict]:
    """Boot cycles in the session: start time, line/error/crash counts per boot.

    A rebooting device shows up here as many short boots — check this first
    when behavior looks wedged."""
    return query.list_boots(_session(session))


@mcp.tool
def get_crash(crash_id: int | None = None, session: str | None = None) -> dict:
    """Full crash artifact (raw panic lines + decoded backtrace when available).

    Defaults to the most recent crash. A decoded frame like
    `app_main at main.c:42` is the highest-value signal in the whole session."""
    reports = query.list_crashes(_session(session))
    if not reports:
        return {"found": False, "hint": "no crashes recorded in this session"}
    if crash_id is None:
        return {"found": True, **reports[-1]}
    for r in reports:
        if r.get("crash_id") == crash_id:
            return {"found": True, **r}
    return {"found": False, "hint": f"no crash with id {crash_id}"}


@mcp.tool
def send_to_device(text: str, newline: bool = True) -> dict:
    """Send a line to the device over serial (stimulus injection for testing)."""
    state = daemon.find_daemon(None)
    if not state:
        return {"ok": False, "error": "no capture daemon running (`hwlog start`)"}
    return daemon.send_cmd(state, {"cmd": "send", "data": text, "newline": newline})


@mcp.tool
def wait_for_pattern(pattern: str, timeout_s: float = 30.0, session: str | None = None) -> dict:
    """Block until PATTERN (regex) appears in NEW device output, or time out.

    The behavioral-verification primitive: after flashing, assert the expected
    log line actually appears instead of assuming success. Max timeout 120s."""
    s = _session(session)
    rx = re.compile(pattern)
    log_path = s / "log.jsonl"
    offset = log_path.stat().st_size if log_path.exists() else 0
    deadline = time.monotonic() + min(timeout_s, 120.0)
    while time.monotonic() < deadline:
        if log_path.exists():
            with log_path.open(encoding="utf-8") as f:
                f.seek(offset)
                chunk = f.read()
                offset = f.tell()
            for line in chunk.splitlines():
                try:
                    rec = query.LogRecord.from_json(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if rx.search(rec.msg):
                    return {"matched": True, "record": query.format_record(rec)}
        time.sleep(0.2)
    return {"matched": False, "hint": f"no /{pattern}/ within {timeout_s}s"}


@mcp.tool
def list_capture_sessions() -> list[dict]:
    """All recorded sessions (oldest first) with port, boot and crash counts."""
    out = []
    for p in list_sessions():
        m = read_meta(p)
        out.append(
            {
                "session_id": m.session_id,
                "port": m.port,
                "started_at": m.started_at,
                "ended_at": m.ended_at,
                "boots": m.boots,
                "crashes": m.crashes,
            }
        )
    return out
