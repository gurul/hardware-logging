from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hwlog import flash as flash_mod
from hwlog import ports, query
from hwlog import session as session_mod
from hwlog.cli import app
from hwlog.crash import MAX_CRASH_CONTENT_CHARS, CrashReport
from hwlog.parsers import parse_line, strip_ansi
from hwlog.records import MAX_MESSAGE_CHARS, MAX_TAG_CHARS, LogRecord, SessionMeta, now_iso
from hwlog.safety import PatternError, compile_pattern, pattern_matches
from hwlog.session import (
    SessionWriter,
    base_dir,
    list_sessions,
    port_slug,
    read_meta,
    resolve_session,
    sessions_dir,
)


def _meta(*, started_at: str = "2026-08-06T12:34:56.789Z") -> SessionMeta:
    return SessionMeta(
        session_id="",
        port="/dev/cu.usbmodem101",
        baud=115200,
        started_at=started_at,
    )


def _record(seq: int, msg: str) -> LogRecord:
    return LogRecord(ts=now_iso(), seq=seq, boot=0, msg=msg)


def test_maximum_parser_record_remains_queryable() -> None:
    parsed = parse_line(f"I (1) {'t' * MAX_TAG_CHARS}: {'😀' * MAX_MESSAGE_CHARS}")
    writer = SessionWriter(_meta())
    try:
        writer.write_record(
            LogRecord(
                ts=now_iso(),
                seq=writer.next_seq(),
                boot=0,
                level=parsed.level,
                tag=parsed.tag,
                msg=parsed.msg,
                dev_ts=parsed.dev_ts,
            )
        )
        records = list(query.iter_records(writer.dir))
    finally:
        writer.close()

    assert len(records) == 1
    assert records[0].tag == parsed.tag
    assert records[0].msg == parsed.msg


def test_json_output_can_escape_a_corrupt_lone_surrogate() -> None:
    record = _record(1, "before\ud800after")

    rendered = record.to_json(ensure_ascii=True)

    assert "\\ud800" in rendered
    rendered.encode("utf-8")


def test_maximum_control_character_record_remains_queryable() -> None:
    writer = SessionWriter(_meta())
    message = "\0" * MAX_MESSAGE_CHARS
    try:
        writer.write_record(_record(writer.next_seq(), message))
        records = list(query.iter_records(writer.dir))
    finally:
        writer.close()

    assert len(records) == 1
    assert records[0].msg == message


def test_session_selectors_reject_traversal_and_symlinks(tmp_path: Path) -> None:
    writer = SessionWriter(_meta())
    writer.close()

    outside = tmp_path / "outside-session"
    outside.mkdir()
    (sessions_dir() / "escape").symlink_to(outside, target_is_directory=True)

    assert resolve_session(writer.dir.name) == writer.dir
    assert resolve_session("../outside-session") is None
    assert resolve_session(str(outside)) is None
    assert resolve_session("escape") is None

    current = base_dir() / "current"
    current.unlink()
    current.symlink_to(outside, target_is_directory=True)
    assert resolve_session() == writer.dir


def test_session_writers_with_same_timestamp_get_unique_directories() -> None:
    first = SessionWriter(_meta())
    second = SessionWriter(_meta())
    try:
        assert first.dir != second.dir
        assert first.meta.session_id != second.meta.session_id
        assert first.dir.is_dir()
        assert second.dir.is_dir()
    finally:
        first.close()
        second.close()


def test_port_slugs_preserve_normalization_collisions_with_a_digest() -> None:
    assert port_slug("A-B") != port_slug("A_B")


def test_same_timestamp_session_fallback_uses_creation_order() -> None:
    writers = [SessionWriter(_meta()) for _ in range(12)]
    try:
        (base_dir() / "current").unlink()
        assert resolve_session() == writers[-1].dir
        assert list_sessions()[-1] == writers[-1].dir
    finally:
        for writer in writers:
            writer.close()


def test_port_resolution_refuses_ambiguous_devices(monkeypatch) -> None:
    boards = [
        ports.Board("/dev/cu.usbmodem101", "A", 0x303A, 1, "A", "Espressif"),
        ports.Board("/dev/cu.usbmodem102", "B", 0x303A, 1, "B", "Espressif"),
    ]
    monkeypatch.setattr(ports, "discover", lambda: boards)

    with pytest.raises(ports.AmbiguousPortError, match="multiple development boards"):
        ports.resolve_port()
    with pytest.raises(ports.AmbiguousPortError, match="ambiguous"):
        ports.resolve_port("usbmodem")
    assert ports.resolve_port("/dev/cu.usbmodem101") is boards[0]


def test_session_storage_is_owner_only_even_with_permissive_umask() -> None:
    previous_umask = os.umask(0)
    writer: SessionWriter | None = None
    try:
        writer = SessionWriter(_meta())
    finally:
        os.umask(previous_umask)

    try:
        directory_modes = {
            base_dir(): 0o700,
            sessions_dir(): 0o700,
            writer.dir: 0o700,
            writer.dir / "crashes": 0o700,
        }
        file_modes = {
            writer.dir / "meta.json": 0o600,
            writer.dir / "log.jsonl": 0o600,
            writer.dir / "raw.log": 0o600,
        }
        for path, expected in {**directory_modes, **file_modes}.items():
            assert stat.S_IMODE(path.stat().st_mode) == expected
    finally:
        writer.close()


def test_iter_records_skips_malformed_lines_and_keeps_later_records(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    valid_before = _record(1, "before").to_json().encode()
    valid_after = _record(2, "after").to_json().encode()
    malformed = [
        b"not json",
        b"[]",
        b'{"ts":"now","seq":"one","boot":0,"msg":"wrong seq type"}',
        b'{"ts":"now","seq":3,"boot":0,"msg":7}',
        b"\xff\xfe",
    ]
    (session / "log.jsonl").write_bytes(b"\n".join([valid_before, *malformed, valid_after]) + b"\n")

    assert [record.msg for record in query.iter_records(session)] == ["before", "after"]


def test_iter_records_uses_a_fixed_end_snapshot(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    path = session / "log.jsonl"
    path.write_text(_record(1, "before").to_json() + "\n", encoding="utf-8")
    records = query.iter_records(session, max_bytes=1024 * 1024)

    assert next(records).msg == "before"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(_record(2, "after snapshot").to_json() + "\n")

    assert list(records) == []


def test_tail_clamps_negative_and_huge_limits() -> None:
    records = [_record(seq, f"line {seq}") for seq in range(query.MAX_TAIL + 100)]

    assert query.tail(iter(records), -1) == []
    bounded = query.tail(iter(records), 10**100)
    assert len(bounded) == query.MAX_TAIL
    assert bounded[0].msg == "line 100"
    assert bounded[-1].msg == f"line {query.MAX_TAIL + 99}"


def test_pathological_regex_is_stopped_by_execution_timeout() -> None:
    compiled = compile_pattern(r"(a+)+$")
    started = time.monotonic()

    with pytest.raises(PatternError, match="execution limit"):
        pattern_matches(compiled, "a" * 10_000 + "!")

    assert time.monotonic() - started < 0.5


def test_huge_counted_repeat_is_rejected_before_regex_compilation() -> None:
    started = time.monotonic()

    with pytest.raises(PatternError, match="repeat count exceeds"):
        compile_pattern("a{999999999}")

    assert time.monotonic() - started < 0.1


def test_combined_counted_repeats_are_rejected_before_compilation() -> None:
    with pytest.raises(PatternError, match="complexity"):
        compile_pattern(r"(?x:a{500}# ignored parenthesis (" + "\n){500}")


def test_nested_counted_repeats_are_rejected_before_compilation() -> None:
    with pytest.raises(PatternError, match="complexity"):
        compile_pattern(r"(a{500}){500}")


def test_sequential_counted_repeats_are_not_multiplied() -> None:
    # Sequential repeats add match work; only nesting multiplies expansion.
    compile_pattern(r"a{100}b{200}")
    compile_pattern(r"[0-9]{50}[a-f]{50}x{50}")


def test_braces_inside_character_classes_are_literals() -> None:
    compile_pattern(r"[a{1500}]")


def test_crash_artifacts_sort_by_numeric_id_and_latest_skips_invalid(tmp_path: Path) -> None:
    session = tmp_path / "session"
    crash_dir = session / "crashes"
    crash_dir.mkdir(parents=True)
    for crash_id in (998, 999, 1000, 1001):
        report = {
            "crash_id": crash_id,
            "first_line": f"panic {crash_id}",
            "lines": [f"panic {crash_id}"],
            "backtrace_addrs": [],
            "decoded_frames": [],
        }
        (crash_dir / f"{crash_id:03d}.json").write_text(json.dumps(report), encoding="utf-8")

    assert [report["crash_id"] for report in query.list_crashes(session)] == [
        998,
        999,
        1000,
        1001,
    ]
    assert query.get_crash(session)["crash_id"] == 1001

    (crash_dir / "1002.json").write_text("not json", encoding="utf-8")
    assert query.get_crash(session)["crash_id"] == 1001


def test_terminal_sequences_controls_and_bidi_marks_are_sanitized() -> None:
    hostile = (
        "ok\x1b[31mRED\x1b[0m|"
        "\x1b]52;c;clipboard-data\x07after|"
        "\x1bPdevice-control-data\x1b\\tail|"
        "\x00\x08ctrl|\u202ebidi\tkept"
    )
    expected = "okRED|after|tail|ctrl|bidi\tkept"

    assert strip_ansi(hostile) == expected
    assert parse_line(f"I (42) app: {hostile}").msg == expected


def test_query_rendering_is_utf8_safe_and_single_line() -> None:
    record = _record(1, "café\ud800\ue000\uffff\u2028温度\u2029ready")
    record.tag = "sensor\udfff\ue000\uffff"

    rendered = query.format_record(record)

    assert "sensor:" in rendered
    assert "café 温度 ready" in rendered
    assert "\ud800" not in rendered
    assert "\udfff" not in rendered
    assert "\ue000" not in rendered
    assert "\uffff" not in rendered
    assert "\u2028" not in rendered
    assert "\u2029" not in rendered
    rendered.encode("utf-8")


def test_log_follower_retains_a_record_written_in_two_pieces(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.touch()
    follower = query.LogFollower(path, from_start=True)
    encoded = _record(1, "split record").to_json().encode()
    split_at = len(encoded) // 2

    with path.open("ab") as stream:
        stream.write(encoded[:split_at])
    assert follower.read() == []

    with path.open("ab") as stream:
        stream.write(encoded[split_at:] + b"\n")
    records = follower.read()
    assert [record.msg for record in records] == ["split record"]


def test_wait_retains_a_matching_record_written_in_two_pieces(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "session"
    session.mkdir()
    path = session / "log.jsonl"
    path.touch()
    encoded = _record(1, "eventual needle").to_json().encode()
    split_at = len(encoded) // 2
    sleep_calls = 0

    def append_next_piece(_delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        with path.open("ab") as stream:
            if sleep_calls == 1:
                stream.write(encoded[:split_at])
            elif sleep_calls == 2:
                stream.write(encoded[split_at:] + b"\n")

    monkeypatch.setattr(query.time, "sleep", append_next_piece)

    record = query.wait_for_record(session, "needle", timeout=1)

    assert record is not None
    assert record.msg == "eventual needle"
    assert sleep_calls == 2


def test_wait_timeout_does_not_block_on_a_fifo_log(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    os.mkfifo(session / "log.jsonl")
    started = time.monotonic()

    assert query.wait_for_record(session, "never", timeout=0.05) is None
    assert time.monotonic() - started < 0.5


def test_deeply_nested_json_is_skipped_without_escaping_the_reader(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    nested = (
        '{"ts":"now","seq":1,"boot":0,"msg":"nested","extra":'
        + "[" * 2000
        + "0"
        + "]" * 2000
        + "}\n"
    )
    (session / "log.jsonl").write_text(nested, encoding="utf-8")

    assert list(query.iter_records(session)) == []


def test_writer_bounds_elf_history_to_what_metadata_can_read() -> None:
    writer = SessionWriter(_meta())
    try:
        for index in range(300):
            writer.add_elf(f"/archive/firmware-{index}.elf")
        persisted = read_meta(writer.dir)
    finally:
        writer.close()

    assert len(persisted.elf_paths) == 256
    assert persisted.elf_paths[0].endswith("firmware-44.elf")
    assert persisted.elf_paths[-1].endswith("firmware-299.elf")


def test_writer_evicts_old_elf_paths_before_metadata_exceeds_its_read_limit() -> None:
    writer = SessionWriter(_meta())
    suffix = "x" * 3900
    try:
        for index in range(30):
            writer.add_elf(f"/archive/{index:03d}-{suffix}.elf")
        writer.note_board(
            vid=0x303A,
            pid=0x1001,
            serial_number="s" * 1024,
            hint="h" * 4096,
            location="l" * 1024,
        )
        persisted = read_meta(writer.dir)
        meta_size = (writer.dir / "meta.json").stat().st_size
    finally:
        writer.close()

    assert meta_size <= 64 * 1024
    assert len(persisted.elf_paths) < 30
    assert persisted.elf_paths[-1].endswith(f"029-{suffix}.elf")
    assert persisted.usb_serial == "s" * 1024
    assert persisted.usb_location == "l" * 1024


def test_crash_queries_use_metadata_without_enumerating_the_directory(monkeypatch) -> None:
    writer = SessionWriter(_meta())
    try:
        for crash_id in range(1, 4):
            writer.write_crash(
                CrashReport(
                    crash_id,
                    f"panic {crash_id}",
                    [f"panic {crash_id}"],
                    [],
                )
            )
        writer.close()
        monkeypatch.setattr(
            query.os,
            "scandir",
            lambda *_args, **_kwargs: pytest.fail("metadata-backed query enumerated crashes"),
        )

        assert [item["crash_id"] for item in query.list_crashes(writer.dir)] == [1, 2, 3]
    finally:
        writer.close()


def test_crash_query_recovers_one_artifact_committed_ahead_of_metadata() -> None:
    writer = SessionWriter(_meta())
    first = CrashReport(1, "panic 1", ["panic 1"], [])
    second = CrashReport(2, "panic 2", ["panic 2"], [])
    try:
        writer.write_crash(first)
        atomic_path = writer.dir / "crashes" / "002.json"
        atomic_path.write_text(json.dumps(second.__dict__), encoding="utf-8")
        writer.close()

        assert [item["crash_id"] for item in query.list_crashes(writer.dir)] == [1, 2]
        assert query.get_crash(writer.dir)["crash_id"] == 2
    finally:
        writer.close()


def test_crash_query_does_not_block_on_a_fifo_artifact() -> None:
    writer = SessionWriter(_meta())
    try:
        writer.meta.crashes = 1
        writer._write_meta()
        fifo = writer.dir / "crashes" / "001.json"
        os.mkfifo(fifo)
        started = time.monotonic()

        assert query.list_crashes(writer.dir) == []
        assert time.monotonic() - started < 0.5
    finally:
        writer.close()


def test_large_symbolized_crash_remains_queryable_with_bounded_frames() -> None:
    writer = SessionWriter(_meta())
    raw_line = "Backtrace: " + "0x400dbeef:0x3ffb0000 " * 300
    report = CrashReport(
        1,
        "Guru Meditation Error",
        ["Guru Meditation Error", raw_line],
        ["0x400dbeef"] * 300,
        ["very_long_function at /source/file.cpp:42 " + "x" * 500] * 500,
    )
    try:
        writer.write_crash(report)
        persisted = query.get_crash(writer.dir, 1)
    finally:
        writer.close()

    assert persisted is not None
    assert persisted["lines"][1] == raw_line
    content_size = len(persisted["first_line"]) + sum(
        len(item)
        for key in ("lines", "backtrace_addrs", "decoded_frames")
        for item in persisted[key]
    )
    assert content_size <= MAX_CRASH_CONTENT_CHARS
    assert len(persisted["decoded_frames"]) < len(report.decoded_frames)


def test_session_writer_rejects_timestamp_path_escape_before_allocating(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ISO-8601"):
        SessionWriter(_meta(started_at="../../escaped"))

    assert not any(tmp_path.glob("escaped-*"))


def test_session_writer_rejects_unpersistable_storage_limit(monkeypatch) -> None:
    monkeypatch.setenv("HWLOG_MAX_SESSION_BYTES", str(1 << 80))

    with pytest.raises(ValueError, match="must be between"):
        SessionWriter(_meta())


def test_hot_path_writes_do_not_take_the_quota_lock_per_write(monkeypatch) -> None:
    monkeypatch.setenv("HWLOG_MAX_TOTAL_BYTES", str(64 * 1024 * 1024))
    writer = SessionWriter(_meta())
    acquisitions = 0
    original_guard = session_mod.storage_quota_guard

    def counting_guard():
        nonlocal acquisitions
        acquisitions += 1
        return original_guard()

    monkeypatch.setattr(session_mod, "storage_quota_guard", counting_guard)
    try:
        for seq in range(200):
            writer.write_raw(b"raw serial chunk\n")
            writer.write_record(_record(seq + 1, f"line {seq}"))
    finally:
        writer.close()

    # 400 small writes fit inside one prepaid allowance chunk: at most one
    # refill plus the close-time release may touch the cross-process lock.
    assert acquisitions <= 2
    monkeypatch.setenv("HWLOG_MAX_SESSION_BYTES", "1024")
    monkeypatch.setenv("HWLOG_MAX_TOTAL_BYTES", str(10 * 1024 * 1024))
    writer = SessionWriter(_meta())
    try:
        writer.write_raw(b"x" * 100)
        writer.write_record(_record(1, "structured"))
        crash_path = writer.write_crash(CrashReport(1, "panic", ["panic"], []))
        persisted = read_meta(writer.dir)
    finally:
        writer.close()

    assert persisted.storage_capped is True
    assert persisted.storage_cap_reason == "session_limit"
    assert persisted.dropped_raw_bytes == 100
    assert persisted.dropped_records == 1
    assert persisted.dropped_crashes == 0
    assert crash_path.is_file()


def test_global_limit_prunes_only_completed_sessions(monkeypatch) -> None:
    old = SessionWriter(_meta(started_at="2026-08-06T12:00:00Z"))
    old.close()
    (old.dir / "bulk.bin").write_bytes(b"x" * 4096)

    monkeypatch.setenv("HWLOG_MAX_SESSION_BYTES", "1024")
    monkeypatch.setenv("HWLOG_MAX_TOTAL_BYTES", "2048")
    current = SessionWriter(_meta(started_at="2026-08-06T13:00:00Z"))
    try:
        assert not old.dir.exists()
        assert current.dir.is_dir()
    finally:
        current.close()


def test_global_limit_never_prunes_an_active_session(monkeypatch) -> None:
    active = SessionWriter(_meta(started_at="2026-08-06T12:00:00Z"))
    (active.dir / "bulk.bin").write_bytes(b"x" * 4096)
    monkeypatch.setenv("HWLOG_MAX_SESSION_BYTES", str(2 * 1024 * 1024))
    monkeypatch.setenv("HWLOG_MAX_TOTAL_BYTES", "2048")
    current = SessionWriter(_meta(started_at="2026-08-06T13:00:00Z"))
    try:
        assert active.dir.is_dir()
        current.write_raw(b"new data")
        assert read_meta(current.dir).storage_cap_reason == "global_limit"
    finally:
        current.close()
        active.close()


def test_global_limit_is_shared_across_active_writers(monkeypatch) -> None:
    total_limit = 1536 * 1024
    monkeypatch.setenv("HWLOG_MAX_SESSION_BYTES", str(2 * 1024 * 1024))
    monkeypatch.setenv("HWLOG_MAX_TOTAL_BYTES", str(total_limit))
    first = SessionWriter(_meta(started_at="2026-08-06T12:00:00Z"))
    second = SessionWriter(_meta(started_at="2026-08-06T13:00:00Z"))
    try:
        first.write_raw(b"a" * 1024 * 1024)
        second.write_raw(b"b" * 1024 * 1024)
        first_meta = read_meta(first.dir)
        second_meta = read_meta(second.dir)
    finally:
        first.close()
        second.close()

    assert sum((writer.dir / "raw.log").stat().st_size for writer in (first, second)) == 1024 * 1024
    assert first_meta.storage_capped is False
    assert second_meta.storage_capped is True
    assert second_meta.storage_cap_reason == "global_limit"


def test_archive_without_daemon_still_obeys_global_limit(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HWLOG_MAX_TOTAL_BYTES", "2048")
    elf = tmp_path / "standalone.elf"
    elf.write_bytes(b"x" * 4096)

    with pytest.raises(ValueError, match="global storage limit"):
        flash_mod.archive_elf(elf)

    archive = base_dir() / "elf-archive"
    assert not list(archive.glob("*.elf"))


def test_evicted_elf_archive_is_deleted_only_inside_owned_archive() -> None:
    archive = session_mod.ensure_private_dir(base_dir() / "elf-archive")
    paths = []
    for index in range(session_mod.MAX_ELF_PATHS + 1):
        path = archive / f"firmware-{index:03d}.elf"
        path.write_bytes(f"elf {index}".encode())
        old = time.time() - session_mod.UNREFERENCED_ELF_GRACE_SECONDS - 1
        os.utime(path, (old, old))
        paths.append(path)

    writer = SessionWriter(_meta())
    try:
        for path in paths:
            writer.add_elf(str(path))
    finally:
        writer.close()

    assert not paths[0].exists()
    assert all(path.is_file() for path in paths[1:])


def test_new_elf_over_global_limit_is_rejected_and_removed(monkeypatch) -> None:
    monkeypatch.setenv("HWLOG_MAX_SESSION_BYTES", "1024")
    monkeypatch.setenv("HWLOG_MAX_TOTAL_BYTES", "2048")
    writer = SessionWriter(_meta())
    archive = session_mod.ensure_private_dir(base_dir() / "elf-archive")
    oversized = archive / "oversized.elf"
    oversized.write_bytes(b"x" * 4096)
    try:
        with pytest.raises(ValueError, match="global storage limit"):
            writer.add_elf(str(oversized))
    finally:
        writer.close()

    assert not oversized.exists()
    assert read_meta(writer.dir).storage_cap_reason == "global_limit"


def test_atomic_replace_fsyncs_file_and_parent_directory(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "private" / "state.json"
    session_mod.ensure_private_dir(target.parent)
    real_fsync = session_mod.os.fsync
    synced_modes = []

    def tracking_fsync(fd: int) -> None:
        synced_modes.append(session_mod.os.fstat(fd).st_mode)
        real_fsync(fd)

    monkeypatch.setattr(session_mod.os, "fsync", tracking_fsync)
    session_mod.atomic_write_text(target, "{}")

    assert any(stat.S_ISREG(mode) for mode in synced_modes)
    assert any(stat.S_ISDIR(mode) for mode in synced_modes)


def test_cli_log_scan_is_byte_bounded_and_warns() -> None:
    writer = SessionWriter(_meta())
    try:
        writer.write_record(_record(1, "old needle " + "x" * 5000))
        writer.write_record(_record(2, "recent"))
        writer.close()

        result = CliRunner().invoke(
            app,
            ["logs", "--grep", "old needle", "--scan-bytes", "1024"],
        )
    finally:
        writer.close()

    assert result.exit_code == 0
    assert "(no matching records)" in result.output
    assert "older matches were omitted" in result.output
    assert "old needle" not in result.output
