# Gates: crash pipeline and agent-loop improvements

OWNS: src/hwlog/**, tests/**, docs/**, README.md, pyproject.toml, uv.lock, GATES.md

Scope: Fix reproduced crash-pipeline defects, make `wait` and boot selection fit the agent loop, triage the reviewer findings, update the docs, and publish to main.

- [x] G1: Crash assembly emits a halted or watchdog crash after an idle period, keeps every backtrace line, falls back to register program counters, and ignores info-level lookalikes.
  CHECK: uv run pytest -q tests/test_crash_pipeline.py
  EXPECT: /\d+ passed/
  EVIDENCE: exit=0; shell=/bin/sh; cwd=<repo>; path=0f6284dab85b/18 entries; output=........                                                                 [100%] | 8 passed in 1.08s

- [x] G2: addr2line selection follows the ELF architecture and finds PlatformIO, ESP-IDF and Arduino toolchains that are not on PATH.
  CHECK: uv run pytest -q tests/test_addr2line.py
  EXPECT: /\d+ passed/
  EVIDENCE: exit=0; shell=/bin/sh; cwd=<repo>; path=0f6284dab85b/18 entries; output=.......                                                                  [100%] | 7 passed in 0.03s

- [x] G3: `wait` and `--grep` match the `tag: message` rendering, and `wait --fail-on-crash` returns exit code 3 with the crash record in CLI and MCP.
  CHECK: uv run pytest -q tests/test_wait_crash.py
  EXPECT: /\d+ passed/
  EVIDENCE: exit=0; shell=/bin/sh; cwd=<repo>; path=0f6284dab85b/18 entries; output=........                                                                 [100%] | 8 passed in 1.16s

- [x] G4: Negative boot selectors count back from the latest boot in CLI and MCP.
  CHECK: uv run pytest -q tests/test_relative_boot.py
  EXPECT: /\d+ passed/
  EVIDENCE: exit=0; shell=/bin/sh; cwd=<repo>; path=0f6284dab85b/18 entries; output=...                                                                      [100%] | 3 passed in 0.35s

- [x] G5: Repository lint, formatting, full test suite and distribution build pass.
  CHECK: uv run ruff check . && uv run ruff format --check . && uv run pytest -q && uv build --no-sources --no-build-isolation
  EXPECT: Successfully built
  EVIDENCE: exit=0; shell=/bin/sh; cwd=<repo>; path=0f6284dab85b/18 entries; output=Successfully built dist/hardware_logging-0.2.0.tar.gz | Successfully built dist/hardware_logging-0.2.0-py3-none-any.whl

- [x] G6: A real firmware ELF on this machine decodes through the auto-discovered PlatformIO toolchain with no addr2line on PATH.
  EVIDENCE: 2026-11-17, macOS arm64. `shutil.which` found none of the candidate names on PATH. For an archived ESP32-S3 Arduino firmware ELF, `elf_machine` returned 94 (Xtensa) and `find_addr2line` selected `~/.platformio/packages/toolchain-xtensa-esp32s3/bin/xtensa-esp32s3-elf-addr2line`. `decode_backtrace([0x4200a6ac], elf)` with no tool argument returned one frame, `0x4200a6ac: app_main at .../cores/esp32/main.cpp:89`; the address came from the toolchain's `nm` output for `app_main`. No RISC-V or ARM firmware ELF was available, so those two architectures are covered by header-level tests only. No physical board was attached.

- [x] G7: README, CLI reference, MCP reference, architecture notes and the bundled skill describe the new behavior and contain no false statement about it.
  EVIDENCE: Reviewed README.md, docs/{cli,mcp,architecture}.md and src/hwlog/assets/SKILL.md against the changed code. Each stated value was read from its constant: idle publication 2 s (`CRASH_IDLE_FLUSH_S`), orphan grace one hour (`ORPHAN_SESSION_GRACE_SECONDS`), cap retry 15 s (`GLOBAL_CAP_RETRY_SECONDS`), toolchain directories (`_TOOLCHAIN_BIN_GLOBS`), ELF globs (`ELF_SEARCH_GLOBS`), exit code 3 and the unchanged-ELF tool set. `hwlog wait --help` and `hwlog logs --help` show `--fail-on-crash`, the exit codes and the negative boot selector; `hwlog version` prints 0.2.0. The arduino-cli example now uses `compile --build-path build --upload`; that command line was not run against a board.

- [x] G8: Every reviewer finding on daemon, session and flash code is either fixed with a regression test or listed as declined with a reason.
  EVIDENCE: Fixed, each with tests in tests/test_storage_recovery.py or tests/test_review_fixes.py: (1) a session whose writer died is prunable after the grace period; (2) a global-limit cap lifts when space frees; (3) nested-repeat validator bypasses (`[](]` classes, verbose mode behind other flag letters) and the `{,N}` form; (4) Zephyr `build/zephyr/zephyr.elf` discovery; (5) signal-killed flasher exit code; (6) exact device-name and macOS `tty.` port matching; (7) arduino-cli and west join the unchanged-ELF set and the arduino-cli doc example builds inside the project. Negative control: 22 of the new tests fail against the pre-change source, and the pre-change validator accepts all four bypass patterns. Declined: (a) a live daemon whose control-socket file disappears becomes unreachable and blocks new captures (daemon.py `_read_valid_state`). Reproduced by the reviewer with a simulated state; the real-world trigger is unverified and the fix changes the authenticated control path, so it is left open. (b) `update_crash` can reserve quota after the writer closes; not reproduced, and the next disk reconcile corrects the counter.
