"""MCP tool behavior — call the underlying functions directly."""

import json
from pathlib import Path
from types import SimpleNamespace

from hwlog import mcp_server


def fn(tool):
    # fastmcp's @mcp.tool returns the original callable in current versions,
    # but older ones return a Tool wrapper exposing .fn — support both.
    return getattr(tool, "fn", tool)


def test_query_logs_bounded(make_session):
    make_session([("I", "app", f"line {i}") for i in range(1000)])
    out = fn(mcp_server.query_logs)(tail=10_000)  # asks for too much
    assert len(out) <= mcp_server.MAX_TAIL


def test_query_logs_is_bounded_by_total_characters(make_session):
    make_session([("I", "app", "x" * 10_000) for _ in range(100)])

    out = fn(mcp_server.query_logs)(tail=500, collapse_repeats=False)

    assert sum(map(len, out)) <= mcp_server.MAX_OUTPUT_CHARS
    assert all(len(line) <= mcp_server.MAX_RECORD_CHARS for line in out)
    assert all(line.startswith("[UNTRUSTED DEVICE OUTPUT]") for line in out)


def test_query_logs_budget_includes_json_escaping(make_session):
    make_session([("I", "app", ('"\\' * 3000)) for _ in range(100)])

    out = fn(mcp_server.query_logs)(tail=500, collapse_repeats=False)

    assert len(json.dumps(out)) <= mcp_server.MAX_OUTPUT_CHARS


def test_query_logs_latest_boot(make_session):
    make_session([("I", "app", "x")])
    out = fn(mcp_server.query_logs)(boot=-1, tail=10)
    assert out and "x" in out[-1]


def test_list_serial_ports_has_an_aggregate_response_budget(monkeypatch):
    escaped = '"\\' * 1600
    boards = [
        SimpleNamespace(
            device=f"/dev/tty-{index}-{escaped}",
            description=escaped,
            vid=0x303A,
            pid=index,
            serial_number=escaped,
            hint=escaped,
            location=escaped,
        )
        for index in range(100)
    ]
    monkeypatch.setattr(mcp_server.ports_mod, "discover", lambda: boards)

    result = fn(mcp_server.list_serial_ports)()
    encoded = json.dumps(result, ensure_ascii=True)

    assert result[0]["truncated"] is True
    assert result[0]["omitted_ports"] > 0
    assert len(encoded) <= mcp_server.MAX_PORT_OUTPUT_CHARS
    assert len(result) < len(boards) + 1


def test_list_boots_has_an_aggregate_response_budget(monkeypatch, tmp_path):
    rows = [
        {
            "boot": index,
            "started": '"\\' * 32,
            "ended": '"\\' * 32,
            "lines": 1,
            "errors": 0,
            "crashes": 0,
        }
        for index in range(500)
    ]
    monkeypatch.setattr(mcp_server, "_session", lambda _selector=None: tmp_path)
    monkeypatch.setattr(mcp_server.query, "list_boots", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(mcp_server.query, "record_scan_is_truncated", lambda *_args: True)

    result = fn(mcp_server.list_boots)()
    encoded = json.dumps(result, ensure_ascii=True)

    assert result[0]["truncated"] is True
    assert result[0]["scan_truncated"] is True
    assert result[0]["omitted_boot_rows"] > 0
    assert len(encoded) <= mcp_server.MAX_BOOT_OUTPUT_CHARS


def test_get_crash_none_recorded(make_session):
    make_session([("I", "app", "x")])
    res = fn(mcp_server.get_crash)()
    assert res["found"] is False


def test_capture_status_not_running():
    res = fn(mcp_server.capture_status)()
    assert res["running"] is False


def test_capture_status_single_daemon_is_bounded_and_keeps_instance_private(monkeypatch):
    token = "single-daemon-instance-token-must-not-leak"
    escaped = '"\\' * 3000
    state = {
        "pid": 123,
        "port": f"\x1b[31m/dev/test\x1b[0m{escaped}",
        "baud": 115200,
        "session": "/session/" + escaped,
        "started_at": "2026-08-07T12:00:00Z",
        "sock": "/private/control.sock",
        "instance": token,
    }
    response = {
        "ok": False,
        "connected": False,
        "error": f"bad response {token} {escaped}",
        "instance": token,
        "private": {"instance": token},
    }
    monkeypatch.setattr(mcp_server.daemon, "list_daemons", lambda: [state])
    monkeypatch.setattr(mcp_server.daemon, "send_cmd", lambda *_args, **_kwargs: response)

    result = fn(mcp_server.capture_status)()
    encoded = json.dumps(result, ensure_ascii=True)

    assert len(encoded) <= mcp_server.MAX_STATUS_OUTPUT_CHARS
    assert token not in encoded
    assert "instance" not in result
    assert "private" not in result
    assert "sock" not in result
    assert result["truncated"] is True
    for key in ("port", "session", "started_at", "error"):
        assert mcp_server._json_size(result[key]) <= mcp_server.MAX_STATUS_FIELD_CHARS
    assert "\x1b[" not in result["port"]


def test_capture_status_multiple_daemons_keeps_newest_rows_within_budget(monkeypatch):
    escaped = '"\\' * 1600
    states = [
        {
            "pid": index + 10,
            "port": f"/dev/tty-{index}-{escaped}",
            "started_at": f"2026-08-07T12:{index:04d}:00Z",
            "instance": f"private-instance-token-{index}",
        }
        for index in range(500)
    ]
    monkeypatch.setattr(mcp_server.daemon, "list_daemons", lambda: states)

    result = fn(mcp_server.capture_status)()
    encoded = json.dumps(result, ensure_ascii=True)

    assert result["ambiguous"] is True
    assert result["total_daemons"] == len(states)
    assert result["truncated"] is True
    assert 0 < len(result["daemons"]) < len(states)
    assert result["daemons"][-1]["pid"] == states[-1]["pid"]
    assert len(encoded) <= mcp_server.MAX_STATUS_OUTPUT_CHARS
    assert "private-instance-token" not in encoded
    assert all("instance" not in row for row in result["daemons"])
    assert all(
        mcp_server._json_size(value) <= mcp_server.MAX_STATUS_FIELD_CHARS
        for row in result["daemons"]
        for value in row.values()
    )


def test_wait_for_pattern_timeout(make_session):
    make_session([("I", "app", "x")])
    res = fn(mcp_server.wait_for_pattern)("nope", timeout_s=0.4)
    assert res["matched"] is False


def test_wait_for_pattern_rejects_oversized_regex(make_session):
    make_session([("I", "app", "x")])
    res = fn(mcp_server.wait_for_pattern)("x" * 513, timeout_s=1)
    assert res == {"matched": False, "error": "pattern exceeds 512 characters"}


def test_wait_for_pattern_bounds_matched_record(make_session, monkeypatch):
    session = make_session([("I", "app", "needle " + "x" * 10_000)])
    record = next(mcp_server.query.iter_records(session))
    monkeypatch.setattr(mcp_server.query, "wait_for_record", lambda *_args, **_kwargs: record)

    res = fn(mcp_server.wait_for_pattern)("needle", timeout_s=0, session=None)

    assert res["matched"] is True
    assert len(json.dumps(res["record"])) <= mcp_server.MAX_RECORD_CHARS


def test_crash_budget_prioritizes_decoded_frames():
    report = {
        "crash_id": 1,
        "first_line": "panic " + "x" * 4000,
        "lines": ["raw " + "x" * 4090 for _ in range(20)],
        "backtrace_addrs": ["0x400dbeef"],
        "decoded_frames": ["app_main at main.c:42"],
    }

    out = mcp_server._bounded_crash(report)

    assert out["decoded_frames"] == ["app_main at main.c:42"]
    assert out["backtrace_addrs"] == ["0x400dbeef"]
    assert len(json.dumps(out)) <= mcp_server.MAX_CRASH_OUTPUT_CHARS


def test_list_capture_sessions_keeps_newest_rows_with_explicit_truncation(monkeypatch):
    paths = [Path(f"/sessions/session-{index}") for index in range(400)]
    escaped = '"\\' * 1600
    metadata = {
        path: SimpleNamespace(
            session_id=path.name,
            port=f"\x1b[31m/dev/test-{index}\x1b[0m-{escaped}",
            started_at=f"2026-08-07T12:{index:04d}:00Z",
            ended_at=None,
            boots=index,
            crashes=index // 2,
        )
        for index, path in enumerate(paths)
    }
    monkeypatch.setattr(mcp_server, "list_sessions", lambda: paths)
    monkeypatch.setattr(mcp_server, "read_meta", metadata.__getitem__)

    result = fn(mcp_server.list_capture_sessions)()
    encoded = json.dumps(result, ensure_ascii=True)

    assert result[0]["truncated"] is True
    assert result[0]["fields_truncated"] is True
    assert result[0]["omitted_sessions"] > 0
    assert result[-1]["session_id"] == paths[-1].name
    assert len(encoded) <= mcp_server.MAX_SESSION_OUTPUT_CHARS
    for row in result[1:]:
        for key in ("session_id", "port", "started_at", "ended_at"):
            assert mcp_server._json_size(row[key]) <= mcp_server.MAX_SESSION_FIELD_CHARS
        assert "\x1b[" not in row["port"]


def test_send_to_device_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("HWLOG_MCP_ALLOW_SEND", raising=False)
    res = fn(mcp_server.send_to_device)("button:press")
    assert res["ok"] is False
    assert "disabled" in res["error"]
