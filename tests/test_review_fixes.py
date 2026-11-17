"""Regression tests for defects found in the flash, port and regex-safety review."""

from __future__ import annotations

import signal
import sys
import time
from types import SimpleNamespace

import pytest

from hwlog import flash, ports
from hwlog.safety import PatternError, compile_pattern

# -- regex validator ---------------------------------------------------------

# Each hides a nested counted repeat from a structure scanner. Compiling one
# without the validator takes tens of seconds and gigabytes of memory.
BYPASSES = [
    "(?:(?:ab{1000}[](]){1000}[](]){1000}",  # "]" first in a class is a literal
    "(?:(?:ab{1000}[^](]){1000}[^](]){1000}",
    "(?wx)(?:(?:ab{1000}#(\n){1000}#(\n){1000}",  # verbose mode behind another flag
    "(?V1x)(?:(?:ab{1000}#(\n){1000}#(\n){1000}",
]


@pytest.mark.parametrize("pattern", BYPASSES)
def test_hidden_nested_repeats_are_rejected_before_compilation(pattern):
    started = time.monotonic()
    with pytest.raises(PatternError):
        compile_pattern(pattern)
    assert time.monotonic() - started < 2.0


def test_plain_nested_repeat_is_still_rejected():
    with pytest.raises(PatternError):
        compile_pattern("(?:a{1000}){1000}")


def test_omitted_lower_bound_counts_as_a_repeat():
    with pytest.raises(PatternError):
        compile_pattern("a{,4294967294}")
    with pytest.raises(PatternError):
        compile_pattern("(?x)a{,5000}")
    assert compile_pattern("a{,3}b").search("xaab") is not None


@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        ("[]x]+ done", "]x] done"),
        ("[^]]{2,8}=ok", "state=ok"),
        ("heap{} free", "heap{} free"),
        (r"wifi: (connected|got ip) \d{1,3}\.\d{1,3}", "wifi: got ip 10.0"),
        ("(?i)SETUP done", "setup DONE"),
        ("(?x) setup \\s done  # comment", "setup done"),
    ],
)
def test_reasonable_patterns_still_compile_and_match(pattern, text):
    assert compile_pattern(pattern).search(text) is not None


# -- ELF discovery -----------------------------------------------------------


def changed_elf(tmp_path, files):
    before = flash._snapshot_elfs(tmp_path)
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF" + name.encode())
    selected = flash._find_changed_elf(tmp_path, before)
    return None if selected is None else selected[0].relative_to(tmp_path.resolve()).as_posix()


def test_zephyr_build_elf_is_discovered(tmp_path):
    assert changed_elf(tmp_path, ["build/zephyr/zephyr.elf"]) == "build/zephyr/zephyr.elf"


def test_zephyr_sysbuild_stays_ambiguous(tmp_path):
    files = ["build/app/zephyr/zephyr.elf", "build/mcuboot/zephyr/zephyr.elf"]
    assert changed_elf(tmp_path, files) is None


def test_existing_layouts_are_still_discovered(tmp_path):
    for index, name in enumerate(
        ["build/app.elf", ".pio/build/esp32dev/firmware.elf", "build/sketch.ino.elf"]
    ):
        root = tmp_path / str(index)
        root.mkdir()
        assert changed_elf(root, [name]) == name


# -- flash exit code ---------------------------------------------------------


def test_signal_killed_flasher_reports_128_plus_signal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    assert flash.run_flash([sys.executable, "-c", script]) == 128 + signal.SIGTERM


def test_ordinary_flasher_exit_code_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert flash.run_flash([sys.executable, "-c", "raise SystemExit(7)"]) == 7
    assert flash.run_flash([sys.executable, "-c", "pass"]) == 0


# -- port resolution ---------------------------------------------------------


def fake_ports(monkeypatch, devices):
    monkeypatch.setattr(
        ports.list_ports,
        "comports",
        lambda: [
            SimpleNamespace(
                device=device,
                description="",
                vid=0x10C4,
                pid=0xEA60,
                serial_number=f"SN{index}",
                location=None,
            )
            for index, device in enumerate(devices)
        ],
    )


def test_exact_device_name_wins_over_substring(monkeypatch):
    fake_ports(monkeypatch, [f"/dev/ttyUSB{i}" for i in range(12)])
    assert ports.resolve_port("ttyUSB1").device == "/dev/ttyUSB1"
    assert ports.resolve_port("/dev/ttyUSB10").device == "/dev/ttyUSB10"
    with pytest.raises(ports.AmbiguousPortError):
        ports.resolve_port("USB1")  # a true substring is still ambiguous


def test_macos_tty_path_resolves_to_the_listed_callout_port(monkeypatch):
    fake_ports(monkeypatch, ["/dev/cu.usbmodem101"])
    assert ports.resolve_port("/dev/tty.usbmodem101").device == "/dev/cu.usbmodem101"
    assert ports.resolve_port("usbmodem").device == "/dev/cu.usbmodem101"
    assert ports.resolve_port("/dev/tty.usbmodem999") is None
