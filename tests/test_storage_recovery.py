"""Storage recovers from a killed writer and from a temporary global cap."""

from __future__ import annotations

import os
import time

from typer.testing import CliRunner

import hwlog.session as session_mod
from hwlog.cli import app
from hwlog.records import SessionMeta, now_iso
from hwlog.session import SessionWriter, list_sessions, read_meta, session_writer_is_live

MIB = 1024 * 1024
runner = CliRunner()


def writer(port):
    return SessionWriter(SessionMeta(session_id="", port=port, baud=115200, started_at=now_iso()))


def kill(w):
    """Drop the file handles as the kernel does for kill -9: no close(), no end time."""
    w._log.close()
    w._raw.close()


def age(path, seconds):
    old = time.time() - seconds
    for name in ("meta.json", "log.jsonl", "raw.log"):
        os.utime(path / name, (old, old))


def test_writer_liveness_follows_the_process(tmp_path):
    w = writer("/dev/A")
    assert session_writer_is_live(w.dir) is True
    kill(w)
    assert session_writer_is_live(w.dir) is False
    assert read_meta(w.dir).ended_at is None
    assert session_writer_is_live(tmp_path / "no-such-session") is None


def test_closed_session_is_not_live():
    w = writer("/dev/A")
    w.close()
    assert session_writer_is_live(w.dir) is False


def test_orphaned_session_is_pruned_after_the_grace_period(monkeypatch):
    monkeypatch.setenv("HWLOG_MAX_TOTAL_BYTES", str(3 * MIB))
    orphan = writer("/dev/A")
    orphan.write_raw(b"x" * (2 * MIB))
    kill(orphan)
    age(orphan.dir, session_mod.ORPHAN_SESSION_GRACE_SECONDS + 60)

    survivor = writer("/dev/B")
    for _ in range(4):
        survivor.write_raw(b"y" * (512 * 1024))

    assert not survivor.meta.storage_capped and survivor.meta.dropped_raw_bytes == 0
    assert [p.name for p in list_sessions()] == [survivor.dir.name]
    survivor.close()


def test_recent_orphan_and_live_session_are_never_pruned(monkeypatch):
    monkeypatch.setenv("HWLOG_MAX_TOTAL_BYTES", str(3 * MIB))
    recent = writer("/dev/A")
    recent.write_raw(b"x" * MIB)
    kill(recent)  # dead, but inside the grace period
    live = writer("/dev/B")
    live.write_raw(b"x" * MIB)
    age(live.dir, session_mod.ORPHAN_SESSION_GRACE_SECONDS + 60)  # old files, live writer

    third = writer("/dev/C")
    for _ in range(4):
        third.write_raw(b"y" * (512 * 1024))

    assert third.meta.storage_capped and third.meta.storage_cap_reason == "global_limit"
    assert {p.name for p in list_sessions()} == {recent.dir.name, live.dir.name, third.dir.name}
    live.close()
    third.close()


def test_global_cap_lifts_when_another_session_completes(monkeypatch):
    monkeypatch.setenv("HWLOG_MAX_TOTAL_BYTES", str(3 * MIB))
    monkeypatch.setattr(session_mod, "GLOBAL_CAP_RETRY_SECONDS", 0.0)
    first = writer("/dev/A")
    first.write_raw(b"x" * (2 * MIB))
    second = writer("/dev/B")
    second.write_raw(b"y" * (1536 * 1024))
    assert second.meta.storage_capped and second.meta.storage_cap_reason == "global_limit"
    dropped = second.meta.dropped_raw_bytes
    assert dropped > 0

    first.close()  # now completed, so pruning it frees space
    second.write_raw(b"z" * 1024)

    assert not second.meta.storage_capped and second.meta.storage_cap_reason is None
    assert second.meta.dropped_raw_bytes == dropped  # the gap stays on record
    assert (second.dir / "raw.log").read_bytes().endswith(b"z" * 1024)
    persisted = read_meta(second.dir)
    assert not persisted.storage_capped and persisted.dropped_raw_bytes == dropped
    second.close()


def test_global_cap_retry_is_rate_limited(monkeypatch):
    monkeypatch.setenv("HWLOG_MAX_TOTAL_BYTES", str(3 * MIB))
    first = writer("/dev/A")
    first.write_raw(b"x" * (2 * MIB))
    second = writer("/dev/B")
    second.write_raw(b"y" * (1536 * 1024))
    assert second.meta.storage_capped
    first.close()
    calls = []
    real = second._refill_allowance
    monkeypatch.setattr(second, "_refill_allowance", lambda n: calls.append(n) or real(n))
    for _ in range(50):
        second.write_raw(b"z" * 1024)  # inside the default retry interval
    assert calls == [] and second.meta.storage_capped
    second.close()


def test_session_cap_stays_final(monkeypatch):
    monkeypatch.setenv("HWLOG_MAX_SESSION_BYTES", str(2 * MIB))
    monkeypatch.setattr(session_mod, "GLOBAL_CAP_RETRY_SECONDS", 0.0)
    w = writer("/dev/A")
    w.write_raw(b"x" * (3 * MIB))
    assert w.meta.storage_cap_reason == "session_limit"
    w.write_raw(b"y")
    assert w.meta.storage_capped and w.meta.storage_cap_reason == "session_limit"
    w.close()


def test_sessions_command_tells_interrupted_from_active():
    dead = writer("/dev/A")
    kill(dead)
    alive = writer("/dev/B")
    out = runner.invoke(app, ["sessions"]).output.splitlines()
    assert any(dead.dir.name in line and "(interrupted)" in line for line in out)
    assert any(alive.dir.name in line and "(active)" in line for line in out)
    alive.close()
