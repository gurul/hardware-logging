"""MCP tool behavior — call the underlying functions directly."""

from hwlog import mcp_server


def fn(tool):
    # fastmcp's @mcp.tool returns the original callable in current versions,
    # but older ones return a Tool wrapper exposing .fn — support both.
    return getattr(tool, "fn", tool)


def test_query_logs_bounded(make_session):
    make_session([("I", "app", f"line {i}") for i in range(1000)])
    out = fn(mcp_server.query_logs)(tail=10_000)  # asks for too much
    assert len(out) <= mcp_server.MAX_TAIL


def test_query_logs_latest_boot(make_session):
    make_session([("I", "app", "x")])
    out = fn(mcp_server.query_logs)(boot=-1, tail=10)
    assert out and "x" in out[-1]


def test_get_crash_none_recorded(make_session):
    make_session([("I", "app", "x")])
    res = fn(mcp_server.get_crash)()
    assert res["found"] is False


def test_capture_status_not_running():
    res = fn(mcp_server.capture_status)()
    assert res["running"] is False


def test_wait_for_pattern_timeout(make_session):
    make_session([("I", "app", "x")])
    res = fn(mcp_server.wait_for_pattern)("nope", timeout_s=0.4)
    assert res["matched"] is False
