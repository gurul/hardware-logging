"""Negative boot selectors count back from the latest boot."""

from __future__ import annotations

from typer.testing import CliRunner

from hwlog import mcp_server, query
from hwlog.cli import app
from hwlog.records import LogRecord, SessionMeta, now_iso
from hwlog.session import SessionWriter

runner = CliRunner()


def fn(tool):
    return getattr(tool, "fn", tool)


def three_boots():
    writer = SessionWriter(
        SessionMeta(session_id="", port="/dev/cu.usbmodem101", baud=115200, started_at=now_iso())
    )
    for boot in range(3):
        writer.write_record(
            LogRecord(
                ts=now_iso(),
                seq=writer.next_seq(),
                boot=boot,
                level="I",
                tag="app",
                msg=f"hello from boot {boot}",
            )
        )
    writer.close()
    return writer.dir


def test_resolve_boot():
    session = three_boots()
    assert query.resolve_boot(session, None) is None
    assert query.resolve_boot(session, 0) == 0
    assert query.resolve_boot(session, 7) == 7  # exact, even when absent
    assert query.resolve_boot(session, -1) == 2
    assert query.resolve_boot(session, -2) == 1
    assert query.resolve_boot(session, -3) == 0
    assert query.resolve_boot(session, -4) == -1  # before boot 0: matches nothing
    assert query.resolve_boot(session, -400) == -1


def test_cli_selects_the_boot_before_the_latest():
    three_boots()
    latest = runner.invoke(app, ["logs", "--boot", "-1"])
    assert "hello from boot 2" in latest.output and "boot 1" not in latest.output
    previous = runner.invoke(app, ["logs", "--boot", "-2"])
    assert previous.exit_code == 0
    assert "hello from boot 1" in previous.output and "boot 2" not in previous.output
    beyond = runner.invoke(app, ["logs", "--boot", "-9"])
    assert beyond.exit_code == 0 and "hello" not in beyond.output
    assert "(no matching records)" in beyond.output


def test_mcp_selects_the_boot_before_the_latest():
    three_boots()
    out = fn(mcp_server.query_logs)(boot=-2, tail=10)
    assert len(out) == 1 and "hello from boot 1" in out[0]
    assert fn(mcp_server.query_logs)(boot=-9, tail=10) == []
