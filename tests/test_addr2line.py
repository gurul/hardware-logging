"""addr2line selection: by ELF architecture, on PATH or in a private toolchain."""

from __future__ import annotations

import os
import struct

import pytest

from hwlog import crash

EM_ARM, EM_XTENSA, EM_RISCV = 40, 94, 243


def write_elf(path, machine, *, big_endian=False):
    ident = b"\x7fELF" + bytes([1, 2 if big_endian else 1, 1]) + bytes(9)
    order = ">" if big_endian else "<"
    path.write_bytes(ident + struct.pack(order + "HH", 2, machine) + bytes(32))
    return str(path)


def install_tool(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    return str(tool)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty home directory and an empty PATH."""
    root = tmp_path / "home"
    root.mkdir()
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("PATH", str(empty))
    return root


def test_elf_machine_reads_both_byte_orders_and_rejects_non_elf(tmp_path):
    assert crash.elf_machine(write_elf(tmp_path / "x.elf", EM_XTENSA)) == EM_XTENSA
    assert crash.elf_machine(write_elf(tmp_path / "r.elf", EM_RISCV)) == EM_RISCV
    assert crash.elf_machine(write_elf(tmp_path / "be.elf", EM_ARM, big_endian=True)) == EM_ARM
    text = tmp_path / "not.elf"
    text.write_text("this is not an ELF file at all")
    assert crash.elf_machine(str(text)) is None
    assert crash.elf_machine(str(tmp_path / "missing.elf")) is None


def test_tool_names_never_cross_architectures(tmp_path):
    riscv = crash.addr2line_names(write_elf(tmp_path / "r.elf", EM_RISCV))
    assert riscv[0] == "riscv32-esp-elf-addr2line"
    assert not any(name.startswith(("xtensa", "arm")) for name in riscv)
    xtensa = crash.addr2line_names(write_elf(tmp_path / "x.elf", EM_XTENSA))
    assert xtensa[0].startswith("xtensa") and not any(n.startswith("riscv") for n in xtensa)
    arm = crash.addr2line_names(write_elf(tmp_path / "a.elf", EM_ARM))
    assert arm[0] == "arm-none-eabi-addr2line"
    # Unknown or absent ELF: the full list, as before.
    assert crash.addr2line_names(None) == crash.ADDR2LINE_CANDIDATES
    assert crash.addr2line_names(str(tmp_path / "missing.elf")) == crash.ADDR2LINE_CANDIDATES


def test_riscv_elf_gets_the_riscv_tool_when_both_toolchains_are_installed(home, tmp_path):
    packages = home / ".platformio" / "packages"
    install_tool(packages / "toolchain-xtensa-esp32s3" / "bin", "xtensa-esp32s3-elf-addr2line")
    riscv_tool = install_tool(
        packages / "toolchain-riscv32-esp" / "bin", "riscv32-esp-elf-addr2line"
    )
    assert crash.find_addr2line(write_elf(tmp_path / "c3.elf", EM_RISCV)) == riscv_tool
    found = crash.find_addr2line(write_elf(tmp_path / "s3.elf", EM_XTENSA))
    assert os.path.basename(found) == "xtensa-esp32s3-elf-addr2line"


def test_private_toolchains_are_found_without_path(home, tmp_path):
    idf = home / ".espressif" / "tools" / "xtensa-esp-elf" / "esp-13.2.0" / "xtensa-esp-elf"
    idf_tool = install_tool(idf / "bin", "xtensa-esp-elf-addr2line")
    arduino = home / "Library" / "Arduino15" / "packages" / "esp32" / "tools"
    arduino_tool = install_tool(
        arduino / "riscv32-esp-elf-gcc" / "1.0" / "bin", "riscv32-esp-elf-addr2line"
    )
    assert crash.find_addr2line(write_elf(tmp_path / "x.elf", EM_XTENSA)) == idf_tool
    assert crash.find_addr2line(write_elf(tmp_path / "r.elf", EM_RISCV)) == arduino_tool


def test_no_matching_tool_returns_none_instead_of_a_wrong_architecture(home, tmp_path):
    packages = home / ".platformio" / "packages"
    install_tool(packages / "toolchain-xtensa-esp32s3" / "bin", "xtensa-esp32s3-elf-addr2line")
    assert crash.find_addr2line(write_elf(tmp_path / "c3.elf", EM_RISCV)) is None
    assert crash.find_addr2line(write_elf(tmp_path / "arm.elf", EM_ARM)) is None


def test_path_wins_over_a_private_toolchain(home, tmp_path, monkeypatch):
    packages = home / ".platformio" / "packages"
    install_tool(packages / "toolchain-riscv32-esp" / "bin", "riscv32-esp-elf-addr2line")
    on_path = install_tool(tmp_path / "bin", "riscv32-esp-elf-addr2line")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert crash.find_addr2line(write_elf(tmp_path / "r.elf", EM_RISCV)) == on_path


def test_generic_addr2line_serves_any_architecture(home, tmp_path, monkeypatch):
    generic = install_tool(tmp_path / "bin", "addr2line")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert crash.find_addr2line(write_elf(tmp_path / "r.elf", EM_RISCV)) == generic
    assert crash.find_addr2line(None) == generic
