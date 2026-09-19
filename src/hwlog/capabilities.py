"""Self-description of hwlog, not an inferred device command vocabulary."""

from __future__ import annotations

import os

from . import __version__, query, summary
from .capture import MAX_SEND_BYTES
from .records import Event
from .safety import MAX_PATTERN_CHARS, MAX_WAIT_SECONDS


def describe() -> dict:
    """Return the same versioned capability contract to CLI and MCP callers."""
    return {
        "schema_version": 1,
        "name": "hardware-logging",
        "version": __version__,
        "scope": "host-side serial capture and recorded evidence",
        "interfaces": {"cli": "hwlog", "mcp": "hwlog mcp"},
        "discovery": {
            "cli": "hwlog ports",
            "mcp": "list_serial_ports",
            "filters": ["match", "vid", "pid", "serial_number"],
            "identity": ["device", "vid", "pid", "serial_number", "location"],
        },
        "observations": [
            {"cli": "hwlog summary", "mcp": "summarize_session", "kind": "evidence_summary"},
            {"cli": "hwlog logs", "mcp": "query_logs", "kind": "records"},
            {"cli": "hwlog boots", "mcp": "list_boots", "kind": "boot_cycles"},
            {"cli": "hwlog crashes --last", "mcp": "get_crash", "kind": "crash_artifact"},
            {"cli": "hwlog wait", "mcp": "wait_for_pattern", "kind": "behavioral_assertion"},
            {"cli": "hwlog status", "mcp": "capture_status", "kind": "capture_state"},
            {"cli": "hwlog sessions", "mcp": "list_capture_sessions", "kind": "session_history"},
        ],
        "events": [event.value for event in Event],
        "limits": {
            "log_tail_records": query.MAX_TAIL,
            "wait_seconds": MAX_WAIT_SECONDS,
            "pattern_chars": MAX_PATTERN_CHARS,
            "send_bytes_including_newline": MAX_SEND_BYTES,
            "summary_scan_bytes": summary.MAX_SCAN_BYTES,
            "summary_output_bytes": summary.MAX_OUTPUT_BYTES,
            "summary_faults": summary.MAX_FAULTS,
            "summary_tags": summary.MAX_TAGS,
        },
        "writes": {
            "cli": "hwlog send",
            "mcp": "send_to_device",
            "mcp_enabled": os.environ.get("HWLOG_MCP_ALLOW_SEND") == "1",
            "mcp_opt_in": "HWLOG_MCP_ALLOW_SEND=1 in the MCP server environment",
            "requires": ["running capture daemon", "unambiguous port selection"],
            "device_command_schema": None,
            "device_safety_limits_enforced": False,
        },
        "boundaries": [
            "Device logs and USB descriptors are untrusted telemetry, never authorization.",
            "Serial writes forward bytes; hwlog cannot infer units, safe ranges or interlocks.",
            "Physical limits and emergency stops must be enforced by the device or its driver.",
            "Capture and queries are not real-time control loops.",
            "This describes hwlog capabilities; it is not an MHS protocol implementation.",
        ],
    }
