import json
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from hwlog import mcp_server, query
from hwlog.cli import app
from hwlog.records import Event, LogRecord


def test_filter_by_level_is_min_severity(make_session):
    s = make_session(
        [
            ("E", "app", "boom"),
            ("W", "app", "hmm"),
            ("I", "app", "fyi"),
            ("D", "app", "dbg"),
            (None, None, "freeform"),
        ]
    )
    got = [r.msg for r in query.filter_records(query.iter_records(s), level="W")]
    assert got == ["boom", "hmm"]


def test_grep_is_case_insensitive(make_session):
    s = make_session([("I", "wifi", "Connected to AP"), ("I", "ble", "advertising")])
    got = [r.msg for r in query.filter_records(query.iter_records(s), grep="connected")]
    assert got == ["Connected to AP"]


def test_tag_filter(make_session):
    s = make_session([("I", "wifi", "a"), ("I", "ble", "b")])
    got = [r.msg for r in query.filter_records(query.iter_records(s), tag="ble")]
    assert got == ["b"]


def test_collapse_repeats(make_session):
    s = make_session([("I", "hb", "beat")] * 347 + [("E", "app", "died")])
    collapsed = list(query.collapse_repeats(query.iter_records(s)))
    assert len(collapsed) == 2
    assert collapsed[0].extra["repeat"] == 347
    assert collapsed[1].extra is None or "repeat" not in (collapsed[1].extra or {})


@pytest.mark.parametrize(
    "changed",
    [{"boot": 1}, {"src": "other.c:42"}, {"extra": {"sensor": 2}}],
)
def test_collapse_preserves_record_context(changed):
    first = LogRecord(ts="2026-10-02T12:00:00.000Z", seq=1, boot=0, msg="ready")
    second = replace(first, seq=2, **changed)

    assert list(query.collapse_repeats([first, second])) == [first, second]


@pytest.mark.parametrize("event", [Event.BOOT, Event.CRASH, Event.STATUS, Event.SENT])
def test_collapse_preserves_lifecycle_events(event):
    first = LogRecord(ts="2026-10-02T12:00:00.000Z", seq=1, boot=0, event=event)
    second = replace(first, seq=2)

    assert list(query.collapse_repeats([first, second])) == [first, second]


def test_collapse_does_not_mutate_source_records():
    first = LogRecord(ts="2026-10-02T12:00:00.000Z", seq=1, boot=0, msg="beat", extra={"sensor": 1})
    second = replace(first, seq=2, ts="2026-10-02T12:00:01.000Z")
    original = second.to_json()

    collapsed = list(query.collapse_repeats([first, second]))

    assert collapsed[0].seq == second.seq
    assert collapsed[0].ts == second.ts
    assert collapsed[0].extra == {"sensor": 1, "repeat": 2}
    assert second.to_json() == original
    assert first.extra == {"sensor": 1}


def test_cli_and_mcp_keep_repeated_messages_in_separate_boots(make_session):
    session = make_session([])
    first = LogRecord(ts="2026-10-02T12:00:00.000Z", seq=1, boot=0, msg="ready")
    second = replace(first, seq=2, boot=1)
    (session / "log.jsonl").write_text(first.to_json() + "\n" + second.to_json() + "\n")

    result = CliRunner().invoke(app, ["logs", "--session", session.name, "--json"])
    assert result.exit_code == 0, result.output
    assert [json.loads(line)["boot"] for line in result.output.splitlines()] == [0, 1]
    tool = getattr(mcp_server.query_logs, "fn", mcp_server.query_logs)
    output = tool(session=session.name)
    assert len(output) == 2
    assert "b0" in output[0] and "b1" in output[1]


@pytest.mark.parametrize("from_start", [False, True])
def test_follower_reads_replaced_file_even_when_it_is_larger(tmp_path, from_start):
    path = tmp_path / "log.jsonl"
    old = LogRecord(ts="2026-10-02T12:00:00.000Z", seq=1, boot=0, msg="old")
    path.write_text(old.to_json() + "\n")
    follower = query.LogFollower(path, from_start=from_start)
    assert [r.msg for r in follower.read()] == (["old"] if from_start else [])
    new = replace(old, seq=2, msg="new record that must be read from the beginning")
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(new.to_json() + "\n")
    replacement.replace(path)

    assert [r.msg for r in follower.read()] == [new.msg]
    assert follower.read() == []


def test_follower_discards_torn_tail_when_file_is_replaced(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_bytes(b'{"ts":"1999-01-01T00:00:00.000Z","seq":99,')
    follower = query.LogFollower(path, from_start=True)
    assert follower.read() == []
    record = LogRecord(ts="2026-10-02T12:00:00.000Z", seq=1, boot=0, msg="new")
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(record.to_json() + "\n")
    replacement.replace(path)

    assert follower.read() == [record]


def test_follower_still_recovers_from_in_place_truncation(tmp_path):
    path = tmp_path / "log.jsonl"
    old = LogRecord(ts="2026-10-02T12:00:00.000Z", seq=1, boot=0, msg="old" * 100)
    path.write_text(old.to_json() + "\n")
    follower = query.LogFollower(path)
    new = replace(old, seq=2, msg="new")
    path.write_text(new.to_json() + "\n")

    assert [r.msg for r in follower.read()] == ["new"]


def test_wait_matches_output_after_log_replacement(make_session, monkeypatch):
    session = make_session([("I", "app", "old")])
    new = LogRecord(ts="2026-10-02T12:00:00.000Z", seq=1, boot=1, msg="setup done " * 20)
    replaced = False

    def replace_during_wait(_delay):
        nonlocal replaced
        if not replaced:
            replacement = session / "replacement.jsonl"
            replacement.write_text(new.to_json() + "\n")
            replacement.replace(session / "log.jsonl")
            replaced = True

    monkeypatch.setattr(query.time, "sleep", replace_during_wait)

    assert query.wait_for_record(session, "setup done", timeout=1) == new


def test_tail_bounds_output(make_session):
    s = make_session([("I", "app", f"line {i}") for i in range(1000)])
    out = query.tail(query.iter_records(s), 50)
    assert len(out) == 50
    assert out[-1].msg == "line 999"


def test_torn_tail_line_is_skipped(make_session):
    s = make_session([("I", "app", "complete")])
    with (s / "log.jsonl").open("a") as f:
        f.write('{"ts": "2026-01-01T00:00:00.000Z", "seq": 99, "bo')  # torn write
    assert [r.msg for r in query.iter_records(s)] == ["complete"]


def test_list_boots_counts(make_session, tmp_path):
    s = make_session([("E", "app", "x"), ("I", "app", "y")])
    rows = query.list_boots(s)
    assert len(rows) == 1
    assert rows[0]["lines"] == 2
    assert rows[0]["errors"] == 1


def test_format_record_compact(make_session):
    s = make_session([("E", "wifi", "connect failed")])
    line = query.format_record(next(iter(query.iter_records(s))))
    assert "E" in line and "wifi:" in line and "connect failed" in line
