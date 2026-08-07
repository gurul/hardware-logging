from __future__ import annotations

import pytest

from hwlog.records import LogRecord, SessionMeta, now_iso
from hwlog.session import SessionWriter


@pytest.fixture(autouse=True)
def hwlog_dir(tmp_path, monkeypatch):
    """Isolate every test's HWLOG_DIR."""
    monkeypatch.setenv("HWLOG_DIR", str(tmp_path / "hwlog-home"))
    return tmp_path / "hwlog-home"


@pytest.fixture
def make_session():
    """Build a recorded session from (level, tag, msg) tuples; returns its path."""

    def _make(lines, port="/dev/cu.usbmodem101"):
        writer = SessionWriter(
            SessionMeta(session_id="", port=port, baud=115200, started_at=now_iso())
        )
        for level, tag, msg in lines:
            writer.write_record(
                LogRecord(
                    ts=now_iso(),
                    seq=writer.next_seq(),
                    boot=writer.meta.boots,
                    level=level,
                    tag=tag,
                    msg=msg,
                )
            )
        writer.close()
        return writer.dir

    return _make
