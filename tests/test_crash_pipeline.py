"""Crash assembly against realistic panic output, and idle publication."""

from __future__ import annotations

import threading
import time

import serial

import hwlog.capture as capture_mod
from hwlog import query
from hwlog.capture import CaptureLoop
from hwlog.crash import CrashAssembler, extract_backtrace_addrs
from hwlog.parsers import parse_line
from hwlog.records import SessionMeta, now_iso
from hwlog.session import SessionWriter, resolve_session

RISCV_PANIC = """\
Guru Meditation Error: Core  0 panic'ed (Load access fault). Exception was unhandled.
Core  0 register dump:
MEPC    : 0x42000e3c  RA      : 0x42000e2e  SP      : 0x3fc8f2a0  GP      : 0x3fc8b400
TP      : 0x3fc8f2f0  T0      : 0x4005890e  T1      : 0x3fc8ed9c  T2      : 0x00000000
MSTATUS : 0x00001881  MTVEC   : 0x40380001  MCAUSE  : 0x00000005  MTVAL   : 0x00000000
Stack memory:
3fc8f2a0: 0x00000000 0x42000e2e 0x3fc8f2f0 0x00000000
Rebooting...""".splitlines()

TASK_WDT = """\
E (10303) task_wdt: Task watchdog got triggered. The following tasks did not reset in time:
E (10303) task_wdt:  - IDLE0 (CPU 0)
E (10303) task_wdt: Print CPU 0 (current core) backtrace
Backtrace: 0x42008b6a:0x3fc9a2f0 0x42008f11:0x3fc9a310
E (10303) task_wdt: Print CPU 1 backtrace
Backtrace: 0x4037a1c2:0x3fc9b2f0 0x42008f11:0x3fc9b310""".splitlines()


def assemble(lines):
    assembler = CrashAssembler()
    reports = []
    for line in lines:
        parsed = parse_line(line)
        report = assembler.feed(parsed.msg, parsed.level)
        if report is not None:
            reports.append(report)
    return reports, assembler


def test_riscv_panic_uses_register_program_counters():
    reports, _ = assemble(RISCV_PANIC)
    assert len(reports) == 1
    # Faulting PC first, then the caller's return address. SP/GP are not code.
    assert reports[0].backtrace_addrs == ["0x42000e3c", "0x42000e2e"]


def test_xtensa_register_pc_is_the_fallback_only():
    no_backtrace = [
        "Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled.",
        "PC      : 0x400d1234  PS      : 0x00060030  A0      : 0x800d5678  A1      : 0x3ffb1f10",
        "EXCVADDR: 0x00000000  LBEG    : 0x4000c2e0  LEND    : 0x4000c2f6  LCOUNT  : 0x00000000",
    ]
    assert extract_backtrace_addrs(no_backtrace) == ["0x400d1234"]
    with_backtrace = [*no_backtrace, "Backtrace: 0x400d9999:0x3ffb1f10 0x400d5678:0x3ffb1f30"]
    assert extract_backtrace_addrs(with_backtrace) == ["0x400d9999", "0x400d5678"]


def test_every_backtrace_line_is_kept_without_duplicates():
    reports, assembler = assemble(TASK_WDT)
    assert reports == [] and assembler.in_crash  # no reboot marker follows a task watchdog
    report = assembler.flush()
    assert report.backtrace_addrs == ["0x42008b6a", "0x42008f11", "0x4037a1c2"]


def test_routine_level_lookalike_does_not_start_a_crash():
    for line in (
        "I (512) main: Task watchdog timeout configured: 5 s",
        "D (513) heap: stack smashing protection enabled",
        "V (514) app: last reset: Brownout detector",
    ):
        _, assembler = assemble([line])
        assert not assembler.in_crash, line


def test_error_level_and_unformatted_lines_still_start_a_crash():
    for line in (
        TASK_WDT[0],
        "W (9) app: assert failed: x != NULL",
        "abort() was called at PC 0x1",
    ):
        _, assembler = assemble([line])
        assert assembler.in_crash, line


def test_routine_line_inside_a_crash_does_not_split_it():
    lines = [RISCV_PANIC[0], RISCV_PANIC[2], "I (1) boot: Watchdog timeout armed", "Rebooting..."]
    reports, _ = assemble(lines)
    assert len(reports) == 1 and len(reports[0].lines) == 4


# -- capture loop ------------------------------------------------------------


class FakeBoard:
    device = "loop://"
    hint = "loopback"


def start_loop(monkeypatch):
    loop_ser = serial.serial_for_url("loop://", timeout=0.05)
    monkeypatch.setattr(capture_mod.ports, "resolve_port", lambda spec=None: FakeBoard())
    writer = SessionWriter(
        SessionMeta(session_id="", port="loop://", baud=115200, started_at=now_iso())
    )
    cap = CaptureLoop(writer, port_spec="loop://", serial_factory=lambda d, b: loop_ser)
    thread = threading.Thread(target=cap.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not cap.connected and time.monotonic() < deadline:
        time.sleep(0.02)
    assert cap.connected
    return loop_ser, cap, thread


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_halted_device_crash_is_published_while_the_port_stays_open(monkeypatch):
    monkeypatch.setattr(capture_mod, "CRASH_IDLE_FLUSH_S", 0.3)
    ser, cap, thread = start_loop(monkeypatch)
    session = resolve_session()
    try:
        # CONFIG_ESP_SYSTEM_PANIC_PRINT_HALT: the dump ends and nothing follows.
        ser.write(b"abort() was called at PC 0x42001234 on core 0\n")
        ser.write(b"Backtrace: 0x40375e2a:0x3fc9a000 0x42001234:0x3fc9a020\n")
        assert wait_for(lambda: len(query.list_crashes(session)) == 1)
        assert cap.connected  # published by idleness, not by closing the port
        crash = query.list_crashes(session)[0]
        assert crash["backtrace_addrs"] == ["0x40375e2a", "0x42001234"]
        # The artifact is written first and its log record second; wait for it.
        assert wait_for(lambda: any(r.event == "crash" for r in query.iter_records(session)))
        assert cap.connected
    finally:
        cap.stop()
        thread.join(timeout=5)
    assert len(query.list_crashes(session)) == 1  # the stop did not publish it twice


def test_idle_flush_waits_for_the_quiet_period(monkeypatch):
    monkeypatch.setattr(capture_mod, "CRASH_IDLE_FLUSH_S", 30.0)
    ser, cap, thread = start_loop(monkeypatch)
    session = resolve_session()
    try:
        ser.write(b"abort() was called at PC 0x42001234 on core 0\n")
        assert wait_for(
            lambda: any(r.msg.startswith("abort()") for r in query.iter_records(session))
        )
        time.sleep(0.4)
        assert query.list_crashes(session) == []  # still assembling
        ser.write(b"Backtrace: 0x40375e2a:0x3fc9a000\nRebooting...\n")
        assert wait_for(lambda: len(query.list_crashes(session)) == 1)
        assert query.list_crashes(session)[0]["backtrace_addrs"] == ["0x40375e2a"]
    finally:
        cap.stop()
        thread.join(timeout=5)


def test_flash_boundary_starts_a_boot_without_a_reset_banner(monkeypatch):
    ser, cap, thread = start_loop(monkeypatch)
    session = resolve_session()
    try:
        ser.write(b"E (10) app: old firmware line\n")
        assert wait_for(
            lambda: any(r.msg == "old firmware line" for r in query.iter_records(session))
        )
        assert cap.pause(timeout=5)
        reopened = serial.serial_for_url("loop://", timeout=0.05)
        cap.serial_factory = lambda d, b: reopened
        # The flasher reset the board while it held the port: no banner arrives.
        cap.resume(awaiting_elf=True, record_flash_boundary=True)
        assert wait_for(lambda: cap.connected)
        reopened.write(b"HELLO\n")
        assert wait_for(lambda: any(r.msg == "HELLO" for r in query.iter_records(session)))
        records = list(query.iter_records(session))
        assert next(r for r in records if r.msg == "old firmware line").boot == 0
        assert next(r for r in records if r.msg == "HELLO").boot == 1
        boot_events = [r for r in records if r.event == "boot"]
        assert len(boot_events) == 1 and boot_events[0].extra["source"] == "flash"

        # A banner that does arrive belongs to the same boot; the next reset is new.
        reopened.write(b"rst:0x15 (USB_UART_CHIP_RESET),boot:0x8 (SPI_FAST_FLASH_BOOT)\n")
        reopened.write(b"I (20) app: after banner\n")
        assert wait_for(lambda: any(r.msg == "after banner" for r in query.iter_records(session)))
        assert next(r for r in query.iter_records(session) if r.msg == "after banner").boot == 1
        monkeypatch.setattr(capture_mod, "BOOT_DEBOUNCE_S", 0.0)
        reopened.write(b"rst:0xc (RTC_SW_CPU_RST)\nI (5) app: real reboot\n")
        assert wait_for(lambda: any(r.msg == "real reboot" for r in query.iter_records(session)))
        assert next(r for r in query.iter_records(session) if r.msg == "real reboot").boot == 2
    finally:
        cap.stop()
        thread.join(timeout=5)
    assert query.resolve_boot(session, -1) == 2


def test_plain_resume_does_not_start_a_boot(monkeypatch):
    ser, cap, thread = start_loop(monkeypatch)
    session = resolve_session()
    try:
        assert cap.pause(timeout=5)
        reopened = serial.serial_for_url("loop://", timeout=0.05)
        cap.serial_factory = lambda d, b: reopened
        cap.resume()  # an unrelated board paused during another board's flash
        assert wait_for(lambda: cap.connected)
        reopened.write(b"still here\n")
        assert wait_for(lambda: any(r.msg == "still here" for r in query.iter_records(session)))
        assert all(r.boot == 0 for r in query.iter_records(session))
    finally:
        cap.stop()
        thread.join(timeout=5)
