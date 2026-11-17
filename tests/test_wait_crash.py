"""Tag-aware matching and the crash-aware wait, through query, CLI and MCP."""

from __future__ import annotations

from typer.testing import CliRunner

from hwlog import mcp_server, query
from hwlog.cli import app
from hwlog.records import Event, LogRecord, SessionMeta, now_iso
from hwlog.session import SessionWriter

runner = CliRunner()


def fn(tool):
    return getattr(tool, "fn", tool)


def build(records):
    """Write (event, level, tag, msg) tuples as one closed session."""
    writer = SessionWriter(
        SessionMeta(session_id="", port="/dev/cu.usbmodem101", baud=115200, started_at=now_iso())
    )
    for event, level, tag, msg in records:
        writer.write_record(
            LogRecord(
                ts=now_iso(),
                seq=writer.next_seq(),
                boot=0,
                event=event,
                level=level,
                tag=tag,
                msg=msg,
            )
        )
    writer.close()
    return writer.dir


CRASH = (Event.CRASH, "E", None, "Guru Meditation Error: Core 0 panic'ed backtrace[2]")
BOOTED = (Event.LOG, "I", "app", "setup done")
WIFI = (Event.LOG, "I", "wifi", "connected to ap")


def test_pattern_copied_from_a_serial_monitor_matches_tag_and_message():
    session = build([WIFI])
    # The parser split "I (12) wifi: connected to ap" into tag and message.
    hit = query.wait_for_record(session, r"wifi: connected", timeout=0.3, from_start=True)
    assert hit is not None and hit.tag == "wifi"
    assert query.wait_for_record(session, r"^connected", timeout=0.3, from_start=True) is not None
    assert query.wait_for_record(session, r"^wifi:", timeout=0.3, from_start=True) is not None
    assert query.wait_for_record(session, r"wifi: nope", timeout=0.2, from_start=True) is None


def test_grep_matches_tag_and_message_rendering():
    session = build([WIFI, BOOTED])
    records = list(query.iter_records(session))
    assert [r.msg for r in query.filter_records(records, grep="WiFi: Conn")] == ["connected to ap"]
    assert [r.msg for r in query.filter_records(records, grep="connected")] == ["connected to ap"]
    assert list(query.filter_records(records, grep="app: connected")) == []
    res = runner.invoke(app, ["logs", "--grep", "wifi: connected"])
    assert res.exit_code == 0 and "connected to ap" in res.output


def test_crash_ends_the_wait_only_when_asked():
    session = build([CRASH])
    plain = query.wait_for_outcome(session, "setup done", 0.2, from_start=True)
    assert plain.reason == "timeout" and plain.record is None
    stopped = query.wait_for_outcome(session, "setup done", 30, from_start=True, stop_on_crash=True)
    assert stopped.reason == "crash" and stopped.record.event == Event.CRASH


def test_a_match_before_the_crash_wins_and_a_crash_before_the_match_fails():
    ok = query.wait_for_outcome(
        build([BOOTED, CRASH]), "setup done", 5, from_start=True, stop_on_crash=True
    )
    assert ok.reason == "matched" and ok.record.msg == "setup done"
    bad = query.wait_for_outcome(
        build([CRASH, BOOTED]), "setup done", 5, from_start=True, stop_on_crash=True
    )
    assert bad.reason == "crash"


def test_a_pattern_that_targets_the_crash_text_is_a_match():
    outcome = query.wait_for_outcome(
        build([CRASH]), "Guru Meditation", 5, from_start=True, stop_on_crash=True
    )
    assert outcome.reason == "matched"


def test_cli_exit_codes_for_wait():
    build([CRASH, BOOTED])
    crashed = runner.invoke(
        app,
        ["wait", "--pattern", "setup done", "--timeout", "30", "--from-start", "--fail-on-crash"],
    )
    assert crashed.exit_code == 3
    assert "crash recorded before /setup done/" in crashed.output
    assert "hwlog crashes --last" in crashed.output
    # Without the flag the contract is unchanged: the later line matches.
    plain = runner.invoke(
        app, ["wait", "--pattern", "setup done", "--timeout", "5", "--from-start"]
    )
    assert plain.exit_code == 0 and "setup done" in plain.output


def test_cli_wait_timeout_is_still_exit_one_with_the_flag():
    build([BOOTED])
    res = runner.invoke(
        app, ["wait", "--pattern", "never", "--timeout", "0.2", "--from-start", "--fail-on-crash"]
    )
    assert res.exit_code == 1


def test_mcp_wait_reports_a_crash(monkeypatch):
    session = build([CRASH])
    real = query.wait_for_outcome
    monkeypatch.setattr(
        mcp_server.query,
        "wait_for_outcome",
        lambda s, p, t, **kw: real(s, p, t, from_start=True, **kw),
    )
    res = fn(mcp_server.wait_for_pattern)("setup done", timeout_s=30, fail_on_crash=True)
    assert res["matched"] is False and res["crashed"] is True
    assert res["record"].startswith("[UNTRUSTED DEVICE OUTPUT]")
    assert "get_crash" in res["hint"]
    quiet = fn(mcp_server.wait_for_pattern)("setup done", timeout_s=0.2)
    assert quiet["matched"] is False and "crashed" not in quiet
    assert session.exists()
