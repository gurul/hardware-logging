"""Reliability regressions for capture shutdown and flash arbitration."""

from __future__ import annotations

import os
import stat
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import pytest

import hwlog.capture as capture_mod
import hwlog.flash as flash_mod
from hwlog import daemon, ports, query
from hwlog.capture import CaptureLoop
from hwlog.crash import CrashReport
from hwlog.records import Event, SessionMeta, now_iso
from hwlog.session import SessionWriter

PANIC = "Guru Meditation Error: Core 1 panic'ed (StoreProhibited)"
BACKTRACE = "Backtrace: 0x400dbeef:0x3ffb0000"
RESET = "rst:0x3 (SW_RESET)"


class FakeBoard:
    device = "fake://board"
    hint = "test board"
    vid = 0x303A
    pid = 0x1001
    serial_number = "reliability-test-board"


class ControlledSerial:
    """Small serial fake whose reads can be held while pause is requested."""

    def __init__(self, chunks: list[bytes] | None = None, *, reads_released: bool = True):
        self._chunks = deque(chunks or [])
        self.read_entered = threading.Event()
        self.release_reads = threading.Event()
        if reads_released:
            self.release_reads.set()
        self.closed = threading.Event()
        self.closed_at: float | None = None
        self.is_open = True
        self.writes: list[bytes] = []

    @property
    def in_waiting(self) -> int:
        return len(self._chunks[0]) if self._chunks else 0

    def read(self, _size: int) -> bytes:
        self.read_entered.set()
        self.release_reads.wait(timeout=5)
        if not self.is_open:
            return b""
        if self._chunks:
            return self._chunks.popleft()
        time.sleep(0.005)
        return b""

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def close(self) -> None:
        self.is_open = False
        self.closed_at = time.monotonic()
        self.closed.set()


def make_writer() -> SessionWriter:
    return SessionWriter(
        SessionMeta(
            session_id="",
            port=FakeBoard.device,
            baud=115200,
            started_at=now_iso(),
        )
    )


def start_capture(monkeypatch, serial_port: ControlledSerial):
    monkeypatch.setattr(capture_mod.ports, "resolve_port", lambda _spec=None: FakeBoard())
    writer = make_writer()
    loop = CaptureLoop(writer, serial_factory=lambda _device, _baud: serial_port)
    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()
    assert serial_port.read_entered.wait(timeout=1)
    assert loop.connected
    return writer, loop, thread


def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def control_ok(command: dict, *, generation: int = 1) -> dict:
    response = {"ok": True}
    if command.get("cmd") == "resume" and command.get("awaiting_elf"):
        response["firmware_generation"] = generation
    return response


def test_pause_acknowledges_only_after_serial_close(monkeypatch):
    serial_port = ControlledSerial(reads_released=False)
    _writer, loop, capture_thread = start_capture(monkeypatch, serial_port)
    result: list[tuple[bool, float]] = []
    pause_thread = threading.Thread(
        target=lambda: result.append((loop.pause(timeout=1), time.monotonic())),
        daemon=True,
    )

    try:
        pause_thread.start()
        assert loop.pause_event.wait(timeout=0.2)
        time.sleep(0.05)

        assert pause_thread.is_alive()
        assert not serial_port.closed.is_set()

        serial_port.release_reads.set()
        pause_thread.join(timeout=1)
        assert result and result[0][0] is True
        assert serial_port.closed_at is not None
        assert serial_port.closed_at <= result[0][1]
    finally:
        serial_port.release_reads.set()
        loop.stop()
        capture_thread.join(timeout=2)
        pause_thread.join(timeout=2)

    assert not capture_thread.is_alive()


def test_pause_during_discovery_prevents_a_late_port_open(monkeypatch):
    discovery_started = threading.Event()
    release_discovery = threading.Event()
    opened: list[str] = []
    writer = make_writer()
    loop = CaptureLoop(
        writer,
        serial_factory=lambda device, _baud: (
            opened.append(device),
            ControlledSerial(),
        )[1],
    )

    def delayed_resolve(_spec=None):
        discovery_started.set()
        release_discovery.wait(timeout=2)
        return FakeBoard()

    monkeypatch.setattr(capture_mod.ports, "resolve_port", delayed_resolve)
    result: list[bool] = []
    opener = threading.Thread(target=lambda: result.append(loop._open_port()), daemon=True)
    opener.start()
    assert discovery_started.wait(timeout=1)

    try:
        assert loop.pause(timeout=0.05) is False
        release_discovery.set()
        opener.join(timeout=1)
        assert result == [False]
        assert opened == []
        assert loop._port_released.is_set()
    finally:
        release_discovery.set()
        opener.join(timeout=1)
        loop._decoder.shutdown(wait=False, cancel_futures=True)
        writer.close()


def test_resume_interrupts_reconnect_backoff():
    writer = make_writer()
    loop = CaptureLoop(writer)
    elapsed: list[float] = []

    def wait() -> None:
        started = time.monotonic()
        loop._wait_for_control(5)
        elapsed.append(time.monotonic() - started)

    waiter = threading.Thread(target=wait, daemon=True)
    waiter.start()
    time.sleep(0.05)
    loop.resume()
    waiter.join(timeout=1)
    try:
        assert elapsed and elapsed[0] < 0.5
    finally:
        loop._decoder.shutdown(wait=False, cancel_futures=True)
        writer.close()


@pytest.mark.parametrize("shutdown", ["pause", "stop"])
def test_shutdown_flushes_unterminated_line_and_active_crash(monkeypatch, shutdown):
    pending = BACKTRACE.encode()
    serial_port = ControlledSerial([PANIC.encode() + b"\n" + pending])
    writer, loop, capture_thread = start_capture(monkeypatch, serial_port)

    try:
        assert wait_for(lambda: loop._crash.in_crash and loop._buf == pending)
        if shutdown == "pause":
            assert loop.pause(timeout=1)
        else:
            loop.stop()
        if shutdown == "pause":
            loop.stop()
        capture_thread.join(timeout=2)
    finally:
        serial_port.release_reads.set()
        loop.stop()
        capture_thread.join(timeout=2)

    assert not capture_thread.is_alive()
    records = list(query.iter_records(writer.dir))
    assert any(record.event == Event.LOG and record.msg == BACKTRACE for record in records)
    assert len([record for record in records if record.event == Event.CRASH]) == 1
    reports = query.list_crashes(writer.dir)
    assert len(reports) == 1
    assert reports[0]["lines"] == [PANIC, BACKTRACE]


def test_crash_ending_at_reset_is_attributed_to_old_boot(monkeypatch):
    writer = make_writer()
    loop = CaptureLoop(writer)
    monkeypatch.setattr(capture_mod.time, "monotonic", lambda: 10.0)
    try:
        loop._handle_line(PANIC)
        loop._handle_line(BACKTRACE)
        loop._handle_line(RESET)
    finally:
        writer.close()

    records = list(query.iter_records(writer.dir))
    crash = next(record for record in records if record.event == Event.CRASH)
    boot = next(record for record in records if record.event == Event.BOOT)
    reset_line = next(
        record for record in records if record.event == Event.LOG and record.msg == RESET
    )
    assert crash.boot == 0
    assert boot.boot == 1
    assert reset_line.boot == 1


def test_boot_debounce_eventually_counts_continuous_markers(monkeypatch):
    writer = make_writer()
    loop = CaptureLoop(writer)
    times = iter([10.0, 11.0, 12.0, 13.1])
    monkeypatch.setattr(capture_mod.time, "monotonic", lambda: next(times))
    try:
        for _ in range(4):
            loop._handle_line(RESET)
    finally:
        writer.close()

    boots = [record for record in query.iter_records(writer.dir) if record.event == Event.BOOT]
    assert [record.boot for record in boots] == [1, 2]
    assert writer.meta.boots == 2


def test_reconnect_refuses_a_different_physical_board(monkeypatch):
    class OtherBoard:
        device = "fake://other"
        hint = "other board"
        vid = 0x303A
        pid = 0x1001
        serial_number = "different-physical-board"

    writer = make_writer()
    loop = CaptureLoop(writer)
    boards = iter([FakeBoard(), OtherBoard()])
    opened: list[str] = []
    monkeypatch.setattr(capture_mod.ports, "resolve_port", lambda _spec=None: next(boards))
    loop.serial_factory = lambda device, _baud: (
        opened.append(device),
        ControlledSerial(),
    )[1]

    try:
        assert loop._open_port() is True
        loop._close_port("test disconnect")
        assert loop._open_port() is False
    finally:
        writer.close()
        loop._decoder.shutdown(wait=False)

    assert opened == [FakeBoard.device]
    assert any(
        "refusing to switch physical boards" in record.msg
        for record in query.iter_records(writer.dir)
    )


def test_reconnect_follows_matching_usb_serial_after_path_and_pid_change(monkeypatch):
    class RenumberedBoard:
        device = "/dev/cu.usbmodem1101"
        hint = "bootloader"
        vid = FakeBoard.vid
        pid = 0x2002
        serial_number = FakeBoard.serial_number

    writer = make_writer()
    loop = CaptureLoop(writer, port_spec="/dev/cu.usbmodem101")
    loop._usb_serial = FakeBoard.serial_number
    loop._usb_vid = FakeBoard.vid
    loop._usb_pid = FakeBoard.pid
    opened: list[str] = []
    monkeypatch.setattr(capture_mod.ports, "resolve_port", lambda _spec=None: None)
    monkeypatch.setattr(capture_mod.ports, "discover", lambda: [RenumberedBoard()])
    loop.serial_factory = lambda device, _baud: (
        opened.append(device),
        ControlledSerial(),
    )[1]

    try:
        assert loop._open_port() is True
        assert opened == [RenumberedBoard.device]
    finally:
        loop._close_port("test complete")
        loop._decoder.shutdown(wait=False, cancel_futures=True)
        writer.close()


def test_pinned_auto_capture_reconnects_after_another_board_appears(monkeypatch):
    pinned = ports.Board(
        "/dev/cu.usbmodem1101",
        "pinned",
        FakeBoard.vid,
        FakeBoard.pid,
        FakeBoard.serial_number,
        FakeBoard.hint,
    )
    other = ports.Board(
        "/dev/cu.usbmodem102",
        "other",
        FakeBoard.vid,
        FakeBoard.pid,
        "other-board",
        FakeBoard.hint,
    )
    writer = make_writer()
    loop = CaptureLoop(writer)
    loop._usb_serial = FakeBoard.serial_number
    loop._usb_vid = FakeBoard.vid
    loop._usb_pid = FakeBoard.pid
    opened: list[str] = []

    def ambiguous(_spec=None):
        raise ports.AmbiguousPortError("multiple development boards found")

    monkeypatch.setattr(capture_mod.ports, "resolve_port", ambiguous)
    monkeypatch.setattr(capture_mod.ports, "discover", lambda: [other, pinned])
    loop.serial_factory = lambda device, _baud: (
        opened.append(device),
        ControlledSerial(),
    )[1]

    try:
        assert loop._open_port() is True
        assert opened == [pinned.device]
    finally:
        loop._close_port("test complete")
        loop._release_device_ownership()
        loop._decoder.shutdown(wait=False, cancel_futures=True)
        writer.close()


def test_unserialized_bridge_keeps_lock_across_path_renumbering(monkeypatch):
    class FirstBoard:
        device = "/dev/cu.usbserial101"
        hint = "bridge"
        vid = 0x10C4
        pid = 0xEA60
        serial_number = None
        location = None

    class RenumberedBoard:
        device = "/dev/cu.usbserial1101"
        hint = "bridge"
        vid = FirstBoard.vid
        pid = FirstBoard.pid
        serial_number = None
        location = None

    writer = make_writer()
    loop = CaptureLoop(writer, port_spec=FirstBoard.device)
    boards = iter([FirstBoard(), None])
    monkeypatch.setattr(capture_mod.ports, "resolve_port", lambda _spec=None: next(boards))
    monkeypatch.setattr(capture_mod.ports, "discover", lambda: [RenumberedBoard()])
    opened: list[str] = []
    loop.serial_factory = lambda device, _baud: (
        opened.append(device),
        ControlledSerial(),
    )[1]

    try:
        assert loop._open_port() is True
        loop._close_port("renumbered")
        assert loop._open_port() is True
        assert opened == [FirstBoard.device, RenumberedBoard.device]
    finally:
        loop._close_port("test complete")
        loop._release_device_ownership()
        loop._decoder.shutdown(wait=False, cancel_futures=True)
        writer.close()


def test_reconnect_retains_location_lock_when_usb_serial_appears(monkeypatch):
    location = "1-2.3"
    first = ports.Board(
        "/dev/cu.usbserial101",
        "bridge",
        0x10C4,
        0xEA60,
        None,
        "bridge",
        location,
    )
    upgraded = ports.Board(
        "/dev/cu.usbserial1101",
        "bridge with serial metadata",
        first.vid,
        first.pid,
        "newly-visible-serial",
        first.hint,
        location,
    )
    writer = make_writer()
    loop = CaptureLoop(writer, port_spec=first.device)
    boards = iter([first, upgraded])
    monkeypatch.setattr(capture_mod.ports, "resolve_port", lambda _spec=None: next(boards))
    opened: list[str] = []
    loop.serial_factory = lambda device, _baud: (
        opened.append(device),
        ControlledSerial(),
    )[1]

    try:
        assert loop._open_port() is True
        original_lock_keys = sorted(loop._device_locks)
        loop._close_port("metadata upgraded")

        assert loop._open_port() is True
        assert opened == [first.device, upgraded.device]
        assert sorted(loop._device_locks) == original_lock_keys
        assert loop._usb_location == location
        assert loop._usb_serial is None
        assert writer.meta.usb_location == location
        assert writer.meta.usb_serial is None
    finally:
        loop._close_port("test complete")
        loop._release_device_ownership()
        loop._decoder.shutdown(wait=False, cancel_futures=True)
        writer.close()


def test_physical_device_lock_survives_pause_close(monkeypatch):
    monkeypatch.setattr(capture_mod.ports, "resolve_port", lambda _spec=None: FakeBoard())
    first_writer = make_writer()
    second_writer = make_writer()
    first = CaptureLoop(first_writer, serial_factory=lambda *_args: ControlledSerial())
    second = CaptureLoop(second_writer, serial_factory=lambda *_args: ControlledSerial())

    try:
        assert first._open_port() is True
        first._close_port("paused")
        assert second._open_port() is False
        first._release_device_ownership()
        assert second._open_port() is True
    finally:
        first._close_port("test complete")
        second._close_port("test complete")
        first._release_device_ownership()
        second._release_device_ownership()
        first._decoder.shutdown(wait=False, cancel_futures=True)
        second._decoder.shutdown(wait=False, cancel_futures=True)
        first_writer.close()
        second_writer.close()


def test_location_owner_blocks_later_serial_identity_alias(monkeypatch):
    location = "1-2.3"
    location_only = ports.Board(
        "/dev/cu.location-only",
        "bridge",
        0x10C4,
        0xEA60,
        None,
        "bridge",
        location,
    )
    serial_aware = ports.Board(
        "/dev/cu.serial-aware",
        "bridge",
        location_only.vid,
        location_only.pid,
        "newly-visible-serial",
        "bridge",
        location,
    )
    first_writer = make_writer()
    second_writer = make_writer()
    first = CaptureLoop(
        first_writer, port_spec="first", serial_factory=lambda *_: ControlledSerial()
    )
    second = CaptureLoop(
        second_writer,
        port_spec="second",
        serial_factory=lambda *_: ControlledSerial(),
    )
    monkeypatch.setattr(
        capture_mod.ports,
        "resolve_port",
        lambda spec=None: location_only if spec == "first" else serial_aware,
    )

    try:
        assert first._open_port() is True
        first._close_port("paused")
        assert second._open_port() is False
    finally:
        first._close_port("test complete")
        second._close_port("test complete")
        first._release_device_ownership()
        second._release_device_ownership()
        first._decoder.shutdown(wait=False, cancel_futures=True)
        second._decoder.shutdown(wait=False, cancel_futures=True)
        first_writer.close()
        second_writer.close()


def test_distinct_serial_boards_can_share_weak_vid_pid_domain(monkeypatch):
    first_board = ports.Board(
        "/dev/cu.board-one", "one", 0x303A, 0x1001, "serial-one", "board", "1-1"
    )
    second_board = ports.Board(
        "/dev/cu.board-two", "two", 0x303A, 0x1001, "serial-two", "board", "1-2"
    )
    first_writer = make_writer()
    second_writer = make_writer()
    first = CaptureLoop(
        first_writer, port_spec="first", serial_factory=lambda *_: ControlledSerial()
    )
    second = CaptureLoop(
        second_writer,
        port_spec="second",
        serial_factory=lambda *_: ControlledSerial(),
    )
    monkeypatch.setattr(
        capture_mod.ports,
        "resolve_port",
        lambda spec=None: first_board if spec == "first" else second_board,
    )

    try:
        assert first._open_port() is True
        assert second._open_port() is True
    finally:
        first._close_port("test complete")
        second._close_port("test complete")
        first._release_device_ownership()
        second._release_device_ownership()
        first._decoder.shutdown(wait=False, cancel_futures=True)
        second._decoder.shutdown(wait=False, cancel_futures=True)
        first_writer.close()
        second_writer.close()


def test_backtrace_symbolization_does_not_block_capture(monkeypatch, tmp_path):
    writer = make_writer()
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"ELF")
    writer.add_elf(str(elf))
    loop = CaptureLoop(writer)
    decode_started = threading.Event()
    release_decode = threading.Event()

    monkeypatch.setattr(capture_mod, "find_addr2line", lambda: "addr2line")

    def blocked_decode(_addresses, _elf_path, _tool):
        decode_started.set()
        release_decode.wait(timeout=2)
        return ["app_main at main.c:42"]

    monkeypatch.setattr(capture_mod, "decode_backtrace", blocked_decode)
    report = CrashReport(
        crash_id=1,
        first_line=PANIC,
        lines=[PANIC, BACKTRACE],
        backtrace_addrs=["0x400dbeef"],
    )

    started = time.monotonic()
    try:
        loop._emit_crash(report)
        elapsed = time.monotonic() - started
        assert elapsed < 0.2
        assert decode_started.wait(timeout=1)
        assert query.get_crash(writer.dir, 1)["decoded_frames"] == []

        loop.note("capture remains responsive")
        release_decode.set()
        assert wait_for(
            lambda: query.get_crash(writer.dir, 1)["decoded_frames"] == ["app_main at main.c:42"]
        )
    finally:
        release_decode.set()
        loop._decoder.shutdown(wait=True)
        writer.close()

    assert any(
        record.msg == "capture remains responsive" for record in query.iter_records(writer.dir)
    )


def test_crash_during_elf_archive_waits_for_new_firmware_symbols(monkeypatch, tmp_path):
    writer = make_writer()
    old_elf = tmp_path / "old.elf"
    new_elf = tmp_path / "new.elf"
    old_elf.write_bytes(b"old")
    new_elf.write_bytes(b"new")
    writer.add_elf(str(old_elf))
    loop = CaptureLoop(writer)
    decoded_with: list[str] = []
    monkeypatch.setattr(capture_mod, "find_addr2line", lambda: "addr2line")

    def decode(_addresses, elf_path, _tool):
        decoded_with.append(elf_path)
        return ["app_main at new.c:42"]

    monkeypatch.setattr(capture_mod, "decode_backtrace", decode)
    report = CrashReport(
        crash_id=1,
        first_line=PANIC,
        lines=[PANIC, BACKTRACE],
        backtrace_addrs=["0x400dbeef"],
    )

    try:
        loop.resume(awaiting_elf=True)
        assert writer.meta.elf_pending is True
        loop._emit_crash(report)
        time.sleep(0.05)
        assert decoded_with == []

        loop.add_elf(str(new_elf))
        assert wait_for(lambda: bool(decoded_with))
        assert decoded_with == [str(new_elf)]
        assert writer.meta.elf_pending is False
    finally:
        loop._decoder.shutdown(wait=True, cancel_futures=True)
        writer.close()


def test_crash_is_never_symbolized_with_a_later_firmware_generation(monkeypatch, tmp_path):
    writer = make_writer()
    loop = CaptureLoop(writer)
    decoded: list[tuple[list[str], str]] = []
    monkeypatch.setattr(capture_mod, "find_addr2line", lambda: "addr2line")

    def decode(addresses, elf_path, _tool):
        decoded.append((addresses, elf_path))
        return ["decoded"]

    monkeypatch.setattr(capture_mod, "decode_backtrace", decode)
    first = CrashReport(1, PANIC, [PANIC, BACKTRACE], ["0x400d0001"])
    second = CrashReport(2, PANIC, [PANIC, BACKTRACE], ["0x400d0002"])
    first_elf = tmp_path / "first.elf"
    first_elf.write_bytes(b"first firmware")
    second_elf = tmp_path / "second.elf"
    second_elf.write_bytes(b"second firmware")

    try:
        first_generation = loop.resume(awaiting_elf=True)
        loop._emit_crash(first)
        second_generation = loop.resume(awaiting_elf=True)
        loop._emit_crash(second)
        with pytest.raises(ValueError, match="stale firmware generation"):
            loop.add_elf(str(first_elf), generation=first_generation)
        loop.add_elf(str(second_elf), generation=second_generation)
        assert wait_for(lambda: bool(decoded))
        assert decoded == [(["0x400d0002"], str(second_elf))]
        assert query.get_crash(writer.dir, 1)["decoded_frames"] == []
    finally:
        loop._decoder.shutdown(wait=True, cancel_futures=True)
        writer.close()


def test_wait_after_flash_includes_output_emitted_before_wait_starts():
    writer = make_writer()
    try:
        loop = CaptureLoop(writer)
        loop.note("before flash")
        loop.resume(awaiting_elf=True)
        loop.note("setup done immediately")

        match = query.wait_for_record(writer.dir, "setup done", timeout=0)
        assert match is not None
        assert match.msg == "setup done immediately"
    finally:
        writer.close()


def test_concurrent_capture_records_have_ordered_unique_sequences():
    writer = make_writer()
    loop = CaptureLoop(writer)
    worker_count = 8
    records_per_worker = 50
    barrier = threading.Barrier(worker_count)

    def emit(worker: int) -> None:
        barrier.wait(timeout=2)
        for index in range(records_per_worker):
            loop.note(f"worker={worker} record={index}")

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            list(pool.map(emit, range(worker_count)))
    finally:
        writer.close()

    records = list(query.iter_records(writer.dir))
    expected_count = worker_count * records_per_worker
    sequences = [record.seq for record in records]
    assert sequences == list(range(1, expected_count + 1))
    assert len({record.msg for record in records}) == expected_count


def test_run_flash_refuses_to_execute_without_pause_acknowledgment(monkeypatch, tmp_path):
    calls: list[dict] = []
    executed = False

    monkeypatch.setattr(flash_mod.daemon, "list_daemons", lambda: [{"daemon": True}])

    def send_cmd(_state, command, **_kwargs):
        calls.append(command.copy())
        return {"ok": False, "error": "serial port still open"}

    def run_command(*_args, **_kwargs):
        nonlocal executed
        executed = True
        raise AssertionError("flash command must not run")

    monkeypatch.setattr(flash_mod.daemon, "send_cmd", send_cmd)
    monkeypatch.setattr(flash_mod.subprocess, "run", run_command)

    with pytest.raises(daemon.DaemonError, match="serial port still open"):
        flash_mod.run_flash(["flash-tool"], cwd=tmp_path)

    assert not executed
    assert [call["cmd"] for call in calls] == ["pause", "resume"]
    assert calls[0]["lease"] == calls[1]["lease"]


def test_run_flash_always_resumes_after_command_exception(monkeypatch, tmp_path):
    calls: list[dict] = []
    monkeypatch.setattr(flash_mod.daemon, "list_daemons", lambda: [{"daemon": True}])

    def send_cmd(_state, command, **_kwargs):
        calls.append(command.copy())
        return control_ok(command)

    def fail_to_start(*_args, **_kwargs):
        raise OSError("flash tool missing")

    monkeypatch.setattr(flash_mod.daemon, "send_cmd", send_cmd)
    monkeypatch.setattr(flash_mod.subprocess, "run", fail_to_start)

    with pytest.raises(OSError, match="flash tool missing"):
        flash_mod.run_flash(["missing-flash-tool"], cwd=tmp_path)

    assert [call["cmd"] for call in calls] == ["pause", "note", "resume"]


def test_failed_flash_does_not_register_an_elf(monkeypatch, tmp_path):
    calls: list[dict] = []
    monkeypatch.setattr(flash_mod.daemon, "list_daemons", lambda: [{"daemon": True}])

    def send_cmd(_state, command, **_kwargs):
        calls.append(command.copy())
        return control_ok(command)

    def unexpected_elf_search(*_args, **_kwargs):
        raise AssertionError("failed flashes must not select or register an ELF")

    monkeypatch.setattr(flash_mod.daemon, "send_cmd", send_cmd)
    monkeypatch.setattr(
        flash_mod.subprocess,
        "run",
        lambda command, cwd, check: subprocess.CompletedProcess(command, 7),
    )
    monkeypatch.setattr(flash_mod, "find_recent_elf", unexpected_elf_search)

    assert flash_mod.run_flash(["flash-tool"], cwd=tmp_path) == 7
    assert "add_elf" not in [call["cmd"] for call in calls]
    assert calls[-1]["cmd"] == "resume"
    assert calls[-1]["awaiting_elf"] is True


def test_concurrent_flash_attempt_is_rejected(monkeypatch, tmp_path):
    entered = threading.Event()
    release = threading.Event()
    first_result: list[int] = []
    first_error: list[BaseException] = []
    state = {"daemon": True, "port": "fake-port"}
    monkeypatch.setattr(flash_mod.daemon, "list_daemons", lambda: [state])
    monkeypatch.setattr(
        flash_mod.daemon,
        "send_cmd",
        lambda _state, command, **_kwargs: control_ok(command),
    )
    monkeypatch.setattr(flash_mod, "find_recent_elf", lambda *_args, **_kwargs: None)

    def blocking_run(command, cwd, check):
        entered.set()
        release.wait(timeout=2)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(flash_mod.subprocess, "run", blocking_run)

    def run_first() -> None:
        try:
            first_result.append(flash_mod.run_flash(["flash-a"], cwd=tmp_path))
        except BaseException as exc:
            first_error.append(exc)

    thread = threading.Thread(target=run_first, daemon=True)
    thread.start()
    assert entered.wait(timeout=1)
    try:
        with pytest.raises(daemon.DaemonError, match="another flash operation"):
            flash_mod.run_flash(["flash-b"], cwd=tmp_path)
    finally:
        release.set()
        thread.join(timeout=2)

    assert not first_error
    assert first_result == [0]


def test_archive_elf_is_owner_only_and_refuses_source_symlinks(tmp_path):
    source = tmp_path / "firmware.elf"
    source.write_bytes(b"ELF payload")

    archived = flash_mod.archive_elf(source)

    assert archived.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(archived.stat().st_mode) == 0o600
    link = tmp_path / "linked.elf"
    link.symlink_to(source)
    with pytest.raises(OSError):
        flash_mod.archive_elf(link)

    selected_digest = flash_mod.hashlib.sha256(source.read_bytes()).hexdigest()
    source.write_bytes(b"changed after selection")
    with pytest.raises(OSError, match="changed after it was selected"):
        flash_mod.archive_elf(source, expected_digest=selected_digest)


def test_successful_flash_does_not_associate_a_stale_elf(monkeypatch, tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    stale = build / "old-target.elf"
    stale.write_bytes(b"stale ELF")
    future = time.time() + 3600
    os.utime(stale, (future, future))
    calls: list[dict] = []
    state = {"daemon": True, "port": "fake-port"}
    monkeypatch.setattr(flash_mod.daemon, "list_daemons", lambda: [state])
    monkeypatch.setattr(
        flash_mod.daemon,
        "send_cmd",
        lambda _state, command, **_kwargs: calls.append(command.copy()) or control_ok(command),
    )
    monkeypatch.setattr(
        flash_mod.subprocess,
        "run",
        lambda command, cwd, check: subprocess.CompletedProcess(command, 0),
    )

    assert flash_mod.run_flash(["flash-tool"], cwd=tmp_path) == 0
    assert "add_elf" not in [call["cmd"] for call in calls]


def test_successful_flash_associates_one_fresh_elf(monkeypatch, tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    calls: list[dict] = []
    writer = make_writer()
    writer.note_board(
        vid=FakeBoard.vid,
        pid=FakeBoard.pid,
        serial_number=FakeBoard.serial_number,
        hint=FakeBoard.hint,
    )
    state = {"daemon": True, "port": FakeBoard.device, "session": str(writer.dir)}
    board = ports.Board(
        FakeBoard.device,
        "target",
        FakeBoard.vid,
        FakeBoard.pid,
        FakeBoard.serial_number,
        FakeBoard.hint,
    )
    monkeypatch.setattr(flash_mod.daemon, "list_daemons", lambda: [state])
    monkeypatch.setattr(flash_mod.ports, "resolve_port", lambda _spec=None: board)
    monkeypatch.setattr(
        flash_mod.daemon,
        "send_cmd",
        lambda _state, command, **_kwargs: calls.append(command.copy()) or control_ok(command),
    )

    def write_fresh_elf(command, cwd, check):
        (build / "app.elf").write_bytes(b"fresh ELF")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(flash_mod.subprocess, "run", write_fresh_elf)

    try:
        assert flash_mod.run_flash(["build-and-flash"], cwd=tmp_path) == 0
    finally:
        writer.close()
    add = next(call for call in calls if call["cmd"] == "add_elf")
    commands = [call["cmd"] for call in calls]
    assert commands.index("resume") < commands.index("add_elf")
    resume = next(call for call in calls if call["cmd"] == "resume")
    assert resume["awaiting_elf"] is True
    assert add["firmware_generation"] == 1
    assert add["path"].endswith(".elf")
    assert flash_mod.Path(add["path"]).read_bytes() == b"fresh ELF"


def test_explicit_flash_pauses_the_only_auto_daemon(monkeypatch, tmp_path):
    writer = make_writer()
    writer.note_board(
        vid=FakeBoard.vid,
        pid=FakeBoard.pid,
        serial_number=FakeBoard.serial_number,
        hint=FakeBoard.hint,
    )
    state = {"port": "auto", "session": str(writer.dir)}
    calls: list[dict] = []
    board = ports.Board(
        FakeBoard.device,
        "target",
        FakeBoard.vid,
        FakeBoard.pid,
        FakeBoard.serial_number,
        FakeBoard.hint,
    )
    monkeypatch.setattr(flash_mod.daemon, "list_daemons", lambda: [state])
    monkeypatch.setattr(flash_mod.ports, "resolve_port", lambda _spec=None: board)
    monkeypatch.setattr(
        flash_mod.daemon,
        "send_cmd",
        lambda _state, command, **_kwargs: calls.append(command.copy()) or control_ok(command),
    )
    monkeypatch.setattr(
        flash_mod.subprocess,
        "run",
        lambda command, cwd, check: subprocess.CompletedProcess(command, 7),
    )

    try:
        assert flash_mod.run_flash(["flash-tool"], port_spec=FakeBoard.device, cwd=tmp_path) == 7
    finally:
        writer.close()

    assert [call["cmd"] for call in calls] == ["status", "pause", "note", "note", "resume"]


def test_flash_invalidates_and_updates_every_physical_alias(monkeypatch, tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    target_writer = make_writer()
    alias_writer = make_writer()
    unknown_writer = make_writer()
    for writer in (target_writer, alias_writer):
        writer.note_board(
            vid=FakeBoard.vid,
            pid=FakeBoard.pid,
            serial_number=FakeBoard.serial_number,
            hint=FakeBoard.hint,
        )
    target = {"name": "target", "port": FakeBoard.device, "session": str(target_writer.dir)}
    alias = {"name": "alias", "port": "auto", "session": str(alias_writer.dir)}
    unknown = {"name": "unknown", "port": "auto", "session": str(unknown_writer.dir)}
    calls: list[tuple[str, dict]] = []
    board = ports.Board(
        FakeBoard.device,
        "target",
        FakeBoard.vid,
        FakeBoard.pid,
        FakeBoard.serial_number,
        FakeBoard.hint,
    )
    monkeypatch.setattr(flash_mod.daemon, "list_daemons", lambda: [target, alias, unknown])
    monkeypatch.setattr(flash_mod.ports, "resolve_port", lambda _spec=None: board)

    def send_cmd(state, command, **_kwargs):
        calls.append((state["name"], command.copy()))
        if command["cmd"] == "status":
            return {"ok": True, "connected": state is target}
        return control_ok(command)

    def flash_command(command, cwd, check):
        (build / "app.elf").write_bytes(b"new aliased firmware")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(flash_mod.daemon, "send_cmd", send_cmd)
    monkeypatch.setattr(flash_mod.subprocess, "run", flash_command)

    try:
        assert (
            flash_mod.run_flash(["build-and-flash"], port_spec=FakeBoard.device, cwd=tmp_path) == 0
        )
    finally:
        target_writer.close()
        alias_writer.close()
        unknown_writer.close()

    resumes = [(name, command) for name, command in calls if command["cmd"] == "resume"]
    adds = [(name, command) for name, command in calls if command["cmd"] == "add_elf"]
    assert {name for name, command in resumes if command["awaiting_elf"]} == {
        "target",
        "alias",
        "unknown",
    }
    assert {name for name, _command in adds} == {"target", "alias"}
    assert {command["firmware_generation"] for _name, command in adds} == {1}


def test_flash_does_not_associate_elf_to_identical_unserialized_alias(monkeypatch, tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    vid, pid = 0x10C4, 0xEA60
    target_writer = make_writer()
    alias_writer = make_writer()
    for writer in (target_writer, alias_writer):
        writer.note_board(
            vid=vid,
            pid=pid,
            serial_number=None,
            location=None,
            hint="unserialized bridge",
        )
    target = {
        "name": "target",
        "port": "/dev/cu.usbserial-target",
        "session": str(target_writer.dir),
    }
    alias = {"name": "alias", "port": "auto", "session": str(alias_writer.dir)}
    board = ports.Board(
        target["port"],
        "target",
        vid,
        pid,
        None,
        "unserialized bridge",
        None,
    )
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(flash_mod.daemon, "list_daemons", lambda: [target, alias])
    monkeypatch.setattr(flash_mod.ports, "resolve_port", lambda _spec=None: board)

    def send_cmd(state, command, **_kwargs):
        calls.append((state["name"], command.copy()))
        if command["cmd"] == "status":
            return {"ok": True, "connected": state is target}
        return control_ok(command)

    def flash_command(command, cwd, check):
        (build / "app.elf").write_bytes(b"target firmware")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(flash_mod.daemon, "send_cmd", send_cmd)
    monkeypatch.setattr(flash_mod.subprocess, "run", flash_command)

    try:
        assert flash_mod.run_flash(["build-and-flash"], port_spec=target["port"], cwd=tmp_path) == 0
    finally:
        target_writer.close()
        alias_writer.close()

    resumes = [(name, command) for name, command in calls if command["cmd"] == "resume"]
    adds = [(name, command) for name, command in calls if command["cmd"] == "add_elf"]
    assert {name for name, command in resumes if command["awaiting_elf"]} == {
        "target",
        "alias",
    }
    assert {name for name, _command in adds} == {"target"}


def test_idf_flash_reuses_identical_known_elf_when_build_is_unchanged(monkeypatch, tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    candidate = build / "app.elf"
    candidate.write_bytes(b"unchanged firmware")
    old = time.time() - 60
    os.utime(candidate, (old, old))
    known = tmp_path / "known"
    known.mkdir()
    archived = known / "previous.elf"
    archived.write_bytes(candidate.read_bytes())
    writer = make_writer()
    writer.add_elf(str(archived))
    writer.note_board(
        vid=FakeBoard.vid,
        pid=FakeBoard.pid,
        serial_number=FakeBoard.serial_number,
        hint=FakeBoard.hint,
    )
    state = {"port": FakeBoard.device, "session": str(writer.dir)}
    calls: list[dict] = []
    monkeypatch.setattr(flash_mod.daemon, "list_daemons", lambda: [state])
    monkeypatch.setattr(flash_mod.ports, "resolve_port", lambda _spec=None: None)
    monkeypatch.setattr(
        flash_mod.daemon,
        "send_cmd",
        lambda _state, command, **_kwargs: calls.append(command.copy()) or control_ok(command),
    )
    monkeypatch.setattr(
        flash_mod.subprocess,
        "run",
        lambda command, cwd, check: subprocess.CompletedProcess(command, 0),
    )

    try:
        assert flash_mod.run_flash(["idf.py", "flash"], cwd=tmp_path) == 0
    finally:
        writer.close()

    add = next(call for call in calls if call["cmd"] == "add_elf")
    assert flash_mod.Path(add["path"]).read_bytes() == candidate.read_bytes()


def test_flash_persisted_notes_redact_command_arguments(monkeypatch, tmp_path):
    secret = "firmware-token-should-not-be-persisted"
    writer = make_writer()
    loop = CaptureLoop(writer)
    monkeypatch.setattr(flash_mod.daemon, "list_daemons", lambda: [{"daemon": True}])

    def send_cmd(_state, command, **_kwargs):
        if command["cmd"] == "note":
            loop.note(command["message"])
        return control_ok(command)

    monkeypatch.setattr(flash_mod.daemon, "send_cmd", send_cmd)
    monkeypatch.setattr(
        flash_mod.subprocess,
        "run",
        lambda command, cwd, check: subprocess.CompletedProcess(command, 0),
    )
    monkeypatch.setattr(flash_mod, "find_recent_elf", lambda _root, since_ts: None)

    try:
        assert flash_mod.run_flash(["flash-tool", "--token", secret], cwd=tmp_path) == 0
    finally:
        writer.close()

    persisted = (writer.dir / "log.jsonl").read_text(encoding="utf-8")
    assert secret not in persisted
    assert "--token" not in persisted
    assert "2 args redacted" in persisted


def test_send_persisted_record_redacts_payload():
    secret = b"wifi_password=do-not-store-this\n"
    writer = make_writer()
    loop = CaptureLoop(writer)
    serial_port = ControlledSerial()
    loop._ser = serial_port

    try:
        assert loop.send(secret)
    finally:
        writer.close()

    persisted = (writer.dir / "log.jsonl").read_text(encoding="utf-8")
    assert secret.decode().strip() not in persisted
    sent = next(record for record in query.iter_records(writer.dir) if record.event == Event.SENT)
    assert sent.msg == f"sent {len(secret)} bytes"
    assert sent.extra == {"bytes": len(secret)}
    assert serial_port.writes == [secret]
