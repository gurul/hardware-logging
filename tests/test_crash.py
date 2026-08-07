import time
from pathlib import Path

import pytest

import hwlog.crash as crash_mod
from hwlog.crash import CrashAssembler, decode_backtrace, is_crash_line

PANIC = "Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled."
BACKTRACE = "Backtrace: 0x400d1234:0x3ffb1234 0x400d5678:0x3ffb5678 0x400dabcd:0x3ffb9abc"


def _fake_addr2line(tmp_path: Path, body: str) -> Path:
    tool = tmp_path / "fake-addr2line"
    tool.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    tool.chmod(0o700)
    return tool


def test_crash_signatures():
    for line in [
        PANIC,
        "abort() was called at PC 0x400d1234",
        "assert failed: do_thing main.c:42",
        "Task watchdog got triggered. The following tasks did not reset the watchdog in time:",
        "CORRUPT HEAP: Bad head at 0x3ffb1234",
        "Brownout detector was triggered",
    ]:
        assert is_crash_line(line), line
    assert not is_crash_line("I (1) main: all good")


def test_assembler_full_artifact():
    ca = CrashAssembler()
    assert ca.feed(PANIC) is None
    assert ca.in_crash
    assert ca.feed("Core  1 register dump:") is None
    assert ca.feed(BACKTRACE) is None
    report = ca.feed("Rebooting...")
    assert report is not None
    assert report.crash_id == 1
    assert report.first_line == PANIC
    assert report.backtrace_addrs == ["0x400d1234", "0x400d5678", "0x400dabcd"]
    assert not ca.in_crash


def test_assembler_flush_on_port_loss():
    ca = CrashAssembler()
    ca.feed(PANIC)
    ca.feed(BACKTRACE)
    report = ca.flush()
    assert report is not None
    assert len(report.backtrace_addrs) == 3


def test_assembler_line_budget():
    ca = CrashAssembler()
    ca.feed(PANIC)
    report = None
    for i in range(300):
        report = ca.feed(f"noise {i}")
        if report:
            break
    assert report is not None  # bounded even without an end marker


def test_normal_watchdog_api_logs_do_not_start_a_crash():
    assembler = CrashAssembler()

    assert assembler.feed("I (42) task_wdt: esp_task_wdt_init(timeout=5)") is None
    assert assembler.feed("I (43) app: stack overflow checks enabled") is None
    assert assembler.in_crash is False


def test_two_crashes_back_to_back():
    ca = CrashAssembler()
    ca.feed(PANIC)
    ca.feed(BACKTRACE)
    first = ca.feed("abort() was called at PC 0x40000000")  # new start ends previous
    assert first is not None and first.crash_id == 1
    assert ca.in_crash
    second = ca.feed("Rebooting...")
    assert second is not None and second.crash_id == 2


def test_decode_without_tool_or_addrs_is_empty():
    assert decode_backtrace([], "/nonexistent.elf") == []
    # Missing ELF: whether or not an addr2line binary exists on this host,
    # decoding must fail soft (empty), never raise.
    assert decode_backtrace(["0x400d1234"], "/nonexistent.elf") == []


def test_decode_replaces_invalid_utf8_from_addr2line(tmp_path):
    tool = _fake_addr2line(
        tmp_path,
        "import os\nos.write(1, b'frame with invalid byte: \\xff\\n')\n",
    )

    frames = decode_backtrace(["0x400d1234"], "firmware.elf", str(tool))

    assert frames == ["frame with invalid byte: �"]


@pytest.mark.parametrize(
    ("fd", "limit_name"),
    [
        (1, "MAX_ADDR2LINE_STDOUT_BYTES"),
        (2, "MAX_ADDR2LINE_STDERR_BYTES"),
    ],
)
def test_decode_kills_infinite_output_at_pipe_ceiling(
    tmp_path,
    monkeypatch,
    fd,
    limit_name,
):
    tool = _fake_addr2line(
        tmp_path,
        f"import os\nfd = {fd}\nchunk = b'x' * 65536\nwhile True:\n    os.write(fd, chunk)\n",
    )
    monkeypatch.setattr(crash_mod, limit_name, 4096)
    monkeypatch.setattr(crash_mod, "ADDR2LINE_TIMEOUT_S", 1.0)

    started = time.monotonic()
    frames = decode_backtrace(["0x400d1234"], "firmware.elf", str(tool))

    assert frames == []
    assert time.monotonic() - started < 0.75


def test_decode_keeps_bounded_prefix_when_stdout_overflows(tmp_path, monkeypatch):
    tool = _fake_addr2line(
        tmp_path,
        "import os\nchunk = b'frame line\\n' * 64\nwhile True:\n    os.write(1, chunk)\n",
    )
    monkeypatch.setattr(crash_mod, "MAX_ADDR2LINE_STDOUT_BYTES", 4096)
    monkeypatch.setattr(crash_mod, "ADDR2LINE_TIMEOUT_S", 1.0)

    frames = decode_backtrace(["0x400d1234"], "firmware.elf", str(tool))

    # A deep but legitimate backtrace keeps its bounded prefix instead of
    # losing the whole decode, and a mid-line cut never yields a torn frame.
    assert frames
    assert all(frame == "frame line" for frame in frames)


def test_decode_enforces_wall_clock_deadline(tmp_path, monkeypatch):
    tool = _fake_addr2line(
        tmp_path,
        "import time\nwhile True:\n    time.sleep(1)\n",
    )
    monkeypatch.setattr(crash_mod, "ADDR2LINE_TIMEOUT_S", 0.2)

    started = time.monotonic()
    frames = decode_backtrace(["0x400d1234"], "firmware.elf", str(tool))

    assert frames == []
    assert 0.15 <= time.monotonic() - started < 0.75


def test_decode_kills_descendant_holding_output_pipes(tmp_path, monkeypatch):
    marker = tmp_path / "descendant-survived"
    tool = _fake_addr2line(
        tmp_path,
        "import os\n"
        "import time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    time.sleep(0.4)\n"
        f"    with open({str(marker)!r}, 'w', encoding='utf-8') as stream:\n"
        "        stream.write('survived')\n"
        "    os._exit(0)\n"
        "os.write(1, b'decoded frame\\n')\n"
        "os._exit(0)\n",
    )
    monkeypatch.setattr(crash_mod, "ADDR2LINE_TIMEOUT_S", 1.0)

    started = time.monotonic()
    frames = decode_backtrace(["0x400d1234"], "firmware.elf", str(tool))
    elapsed = time.monotonic() - started

    assert frames == ["decoded frame"]
    assert elapsed < 0.75
    time.sleep(0.55)
    assert not marker.exists()
