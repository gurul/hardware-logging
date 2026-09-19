import json

import pytest
from typer.testing import CliRunner

from hwlog import mcp_server, query, summary
from hwlog.cli import app
from hwlog.records import LogRecord

runner = CliRunner()


def fn(tool):
    return getattr(tool, "fn", tool)


def append_record(session, **kwargs):
    record = LogRecord(ts="2026-09-18T12:00:00.000Z", **kwargs)
    with (session / "log.jsonl").open("a") as stream:
        stream.write(record.to_json() + "\n")


def test_counts_faults_and_cli_mcp_parity(make_session):
    session = make_session(
        [("I", "app", "ready"), ("W", "sensor", "slow"), ("E", "sensor", "failed")]
    )
    append_record(session, seq=4, boot=1, event="boot", msg="reset")
    append_record(session, seq=5, boot=1, event="crash", msg="panic")
    result = summary.summarize(session)
    assert result["counts"] == {
        "records": 5,
        "errors": 1,
        "warnings": 1,
        "boot_events": 1,
        "crashes": 1,
    }
    assert result["latest_boot"] == 1
    assert [row["msg"] for row in result["recent_faults"]] == ["slow", "failed", "panic"]
    assert result["tags"] == [
        {"tag": "sensor", "records": 2, "text_truncated": False},
        {"tag": "app", "records": 1, "text_truncated": False},
    ]
    assert result["scan"]["available"] is True
    assert result["scan"]["truncated"] is False
    assert result["scan"]["skipped_records"] == 0
    assert result["scan"]["incomplete_tail"] is False
    cli = runner.invoke(app, ["summary", "--session", session.name, "--json"])
    assert cli.exit_code == 0, cli.output
    assert (
        json.loads(cli.output) == fn(mcp_server.summarize_session)(session=session.name) == result
    )
    human = runner.invoke(app, ["summary", "--session", session.name])
    assert human.exit_code == 0
    assert "errors=1" in human.output and "[UNTRUSTED DEVICE OUTPUT]" in human.output
    assert "does not prove device health" in human.output


def test_tail_scan_reports_partial_counts_and_zero_budget(make_session):
    session = make_session([("E", "old", "old failure"), ("I", "new", "recent success")])
    lines = (session / "log.jsonl").read_bytes().splitlines(keepends=True)
    result = summary.summarize(session, scan_bytes=len(lines[-1]))
    assert result["counts"]["records"] == 1
    assert result["counts"]["errors"] == 0
    assert result["last_record"]["msg"] == "recent success"
    assert result["scan"]["truncated"] is True
    assert result["scan"]["scanned_bytes"] == len(lines[-1])
    assert summary.summarize(session)["counts"]["errors"] == 1
    empty_window = summary.summarize(session, scan_bytes=0)
    assert empty_window["counts"]["records"] == 0
    assert empty_window["scan"]["truncated"] is True
    assert empty_window["latest_boot"] is None
    # A cut inside a record must not manufacture a valid partial record.
    assert summary.summarize(session, scan_bytes=len(lines[-1]) - 1)["counts"]["records"] == 0


def test_storage_and_firmware_evidence(make_session):
    session = make_session([])
    meta_path = session / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta.update(
        storage_capped=True,
        storage_cap_reason="session_limit",
        dropped_records=42,
        dropped_raw_bytes=512,
        dropped_crashes=2,
        firmware_generation=3,
        elf_generation=2,
        elf_pending=True,
    )
    meta_path.write_text(json.dumps(meta))
    result = summary.summarize(session)
    assert result["firmware"] == {"generation": 3, "elf_generation": 2, "elf_pending": True}
    assert result["storage"] == {
        "capped": True,
        "cap_reason": "session_limit",
        "dropped_records": 42,
        "dropped_raw_bytes": 512,
        "dropped_crashes": 2,
    }
    assert result["counts"]["records"] == 0
    assert result["last_record"] is None


def test_corrupt_oversized_and_torn_records_are_visible(make_session):
    session = make_session([("I", "app", "valid")])
    with (session / "log.jsonl").open("ab") as stream:
        stream.write(b"not json\n")
        stream.write(b"x" * (query.MAX_JSON_LINE_BYTES + 10) + b"\n")
        stream.write(b'{"seq":')
    result = summary.summarize(session)
    assert result["counts"]["records"] == 1
    assert result["scan"]["skipped_records"] == 3
    assert result["scan"]["incomplete_tail"] is True


def test_unavailable_and_symlink_log_not_reported_as_available(make_session, tmp_path):
    session = make_session([("E", "app", "present")])
    assert summary.summarize(session)["scan"]["available"] is True
    log = session / "log.jsonl"
    log.unlink()
    assert summary.summarize(session)["scan"]["available"] is False
    target = tmp_path / "external.jsonl"
    target.write_text(LogRecord(ts="now", seq=1, boot=0, msg="external").to_json() + "\n")
    log.symlink_to(target)
    result = summary.summarize(session)
    assert result["scan"]["available"] is False
    assert result["counts"]["records"] == 0


@pytest.mark.parametrize("terminated", [False, True])
def test_oversized_tail_coverage_even_when_scan_starts_inside_it(make_session, terminated):
    session = make_session([])
    (session / "log.jsonl").write_bytes(
        b"x" * (query.MAX_JSON_LINE_BYTES + 10) + (b"\n" if terminated else b"")
    )
    full = summary.summarize(session)
    assert full["scan"]["skipped_records"] == 1
    assert full["scan"]["incomplete_tail"] is not terminated
    partial = summary.summarize(session, scan_bytes=10)
    assert partial["scan"]["truncated"] is True
    assert partial["scan"]["incomplete_tail"] is not terminated
    assert partial["counts"]["records"] == 0


def test_output_is_bounded_sanitized_and_retains_newest_faults(make_session):
    payload = "\x1b[31m" + '\U0001f600"\\' * 1000
    session = make_session([("E", f"{i}-{payload[:1000]}", f"{i}-{payload}") for i in range(80)])
    result = summary.summarize(session)
    assert len(json.dumps(result, ensure_ascii=True).encode()) <= summary.MAX_OUTPUT_BYTES
    assert len(result["recent_faults"]) == summary.MAX_FAULTS
    assert result["recent_faults"][-1]["msg"].startswith("79-")
    assert all(row["text_truncated"] for row in result["recent_faults"])
    assert len(result["tags"]) == summary.MAX_TAGS
    assert result["untracked_tag_records"] == 80 - summary.MAX_TAGS
    assert result["omitted_faults"] == 80 - summary.MAX_FAULTS
    assert all("\x1b" not in row["msg"] for row in result["recent_faults"])
    assert result["trust"] == "untrusted_device_output"


def test_scan_ceiling_is_applied(make_session, monkeypatch):
    session = make_session([("I", "app", "x")])
    original = query.iter_records
    budgets = []

    def spy(path, *, max_bytes, stats):
        budgets.append(max_bytes)
        return original(path, max_bytes=max_bytes, stats=stats)

    monkeypatch.setattr(query, "iter_records", spy)
    result = summary.summarize(session, scan_bytes=10**12)
    assert budgets == [summary.MAX_SCAN_BYTES]
    assert result["scan"]["budget_bytes"] == summary.MAX_SCAN_BYTES


def test_session_selection_is_explicit(make_session):
    first = make_session([("E", "app", "first")])
    second = make_session([("I", "app", "second")])
    assert fn(mcp_server.summarize_session)(session=first.name)["counts"]["errors"] == 1
    assert fn(mcp_server.summarize_session)(session=second.name)["counts"]["errors"] == 0
    assert runner.invoke(app, ["summary", "--session", "../outside"]).exit_code == 2


def test_bad_metadata_and_missing_session_fail_cleanly(make_session):
    assert runner.invoke(app, ["summary"]).exit_code == 2
    session = make_session([])
    (session / "meta.json").write_text("not json")
    result = runner.invoke(app, ["summary"])
    assert result.exit_code == 2 and "error:" in result.output


@pytest.mark.parametrize("budget", [-1, True, 1.5])
def test_invalid_summary_budget(make_session, budget):
    with pytest.raises(ValueError):
        summary.summarize(make_session([]), scan_bytes=budget)
