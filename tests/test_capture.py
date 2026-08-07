"""End-to-end capture tests over a pyserial loopback (no hardware needed)."""

from __future__ import annotations

import threading
import time

import serial

import hwlog.capture as capture_mod
from hwlog import query
from hwlog.capture import CaptureLoop
from hwlog.records import SessionMeta, now_iso
from hwlog.session import SessionWriter, resolve_session


class FakeBoard:
    device = "loop://"
    hint = "loopback"


def start_loop(monkeypatch):
    loop_ser = serial.serial_for_url("loop://", timeout=0.1)
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


def test_capture_records_parses_and_segments(monkeypatch):
    ser, cap, thread = start_loop(monkeypatch)
    ser.write(b"I (100) main: hello\n")
    ser.write(b"rst:0x3 (SW_RESET)\n")
    ser.write(b"E (5) init: bad config\n")

    session = resolve_session()
    assert wait_for(lambda: any(r.msg == "bad config" for r in query.iter_records(session)))
    cap.stop()
    thread.join(timeout=5)

    recs = list(query.iter_records(session))
    hello = next(r for r in recs if r.msg == "hello")
    assert hello.boot == 0 and hello.level == "I" and hello.tag == "main"
    bad = next(r for r in recs if r.msg == "bad config")
    assert bad.boot == 1  # segmented by the reset marker
    assert any(r.event == "boot" for r in recs)


def test_capture_assembles_crash(monkeypatch):
    ser, cap, thread = start_loop(monkeypatch)
    ser.write(b"Guru Meditation Error: Core 1 panic'ed (StoreProhibited)\n")
    ser.write(b"Backtrace: 0x400dbeef:0x3ffb0000\n")
    ser.write(b"Rebooting...\n")

    session = resolve_session()
    assert wait_for(lambda: len(query.list_crashes(session)) == 1)
    cap.stop()
    thread.join(timeout=5)

    crash = query.list_crashes(session)[0]
    assert crash["backtrace_addrs"] == ["0x400dbeef"]
    assert any(r.event == "crash" for r in query.iter_records(session))


def test_send_writes_to_device_and_records(monkeypatch):
    ser, cap, thread = start_loop(monkeypatch)
    assert cap.send(b"button:press\n")
    session = resolve_session()
    # loopback: the sent bytes come back as device output too
    assert wait_for(lambda: any(r.event == "sent" for r in query.iter_records(session)))
    cap.stop()
    thread.join(timeout=5)


def test_pause_releases_port_and_resume_reopens(monkeypatch):
    ser, cap, thread = start_loop(monkeypatch)
    cap.pause()
    assert wait_for(lambda: not cap.connected)
    reopened = serial.serial_for_url("loop://", timeout=0.1)
    monkeypatch_factory_called = []

    cap.serial_factory = lambda d, b: (monkeypatch_factory_called.append(1), reopened)[1]
    cap.resume()
    assert wait_for(lambda: cap.connected)
    assert monkeypatch_factory_called
    cap.stop()
    thread.join(timeout=5)


def test_capture_survives_oversized_device_timestamp(monkeypatch):
    ser, cap, thread = start_loop(monkeypatch)
    hostile = "I (" + "9" * 5000 + ") app: hostile"
    session = resolve_session()
    try:
        ser.write(hostile.encode() + b"\n")
        ser.write(b"I (2) app: capture continued\n")

        assert wait_for(
            lambda: any(record.msg == "capture continued" for record in query.iter_records(session))
        )
        assert thread.is_alive()
    finally:
        cap.stop()
        thread.join(timeout=5)

    records = list(query.iter_records(session))
    fallback = next(record for record in records if record.msg == hostile)
    assert fallback.level is None
    assert fallback.dev_ts is None
