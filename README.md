# hardware-logging

Structured, crash-aware serial logging for embedded boards — built so AI coding agents can debug firmware recursively.

`hwlog` runs a small daemon that owns your board's serial port and records everything to structured sessions on disk. Your coding agent (Claude Code, Cursor, anything) never touches the port — it queries the recording through bounded CLI commands or native MCP tools, flashes through a port-safe wrapper, and verifies behavior instead of assuming "it compiled" means "it works."

```
┌──────────┐  serial   ┌──────────────┐   JSONL    ┌─────────────────────┐
│  ESP32 / │ ────────► │ hwlog daemon │ ─────────► │  session on disk    │
│  any MCU │  ◄──────  │ (owns port)  │            │  logs · boots ·     │
└──────────┘   send    └──────┬───────┘            │  decoded crashes    │
                              │ pause/resume       └──────────┬──────────┘
                       ┌──────┴───────┐                       │ bounded queries
                       │ hwlog flash  │            ┌──────────┴──────────┐
                       │ -- idf.py …  │            │  coding agent       │
                       └──────────────┘            │  (CLI or MCP tools) │
                                                   └─────────────────────┘
```

## Why

Wiring a coding agent to a dev board fails in predictable ways: blocking monitors hang the agent, flashing fights the monitor for the port (and looks exactly like a bricked board), ESP32-S3 native USB drops all output until DTR is asserted, ports renumber on replug, crashes scroll away before anyone reads them, and a raw log dump blows the agent's context window. `hwlog` packages the fixes — learned from real hardware incidents — into one tool.

## Features

- **Persistent capture sessions** — logs are recorded to disk continuously; the crash that happened while your agent was thinking is still there
- **Structure at ingest** — ANSI stripped; ESP-IDF and Arduino log formats parsed into `{level, tag, msg, timestamp}`; everything else passes through
- **Boot-cycle segmentation** — "show me logs since the last boot" is one flag (`--boot -1`); reboot loops are instantly visible in `hwlog boots`
- **Crash reports, assembled and decoded** — panics/watchdogs/heap corruption are detected, captured as complete multi-line artifacts, and symbolized with `addr2line` against ELFs archived at flash time
- **Bounded, agent-budget-aware queries** — line, byte, scan, regex-runtime, and timeout ceilings plus repeated-line collapse (`heartbeat (×347)`)
- **Flash-safe port arbitration** — `hwlog flash -- <cmd>` holds an exclusive pause lease, invalidates stale symbols on every attempt, resumes when the tool exits, and conservatively archives one generation-bound ELF candidate
- **Behavioral verification** — `hwlog wait --pattern "setup done" --timeout 20` includes output captured since the latest flash boundary and provides CI-friendly exit codes
- **MCP server + bundled agent skill** — `hwlog mcp` exposes everything as native agent tools; `hwlog init` installs a debug playbook (crash-signature triage, loop protocol) into your project

Works with anything that talks serial: ESP32 family first-class, plus RP2040, STM32, nRF, Arduino — identified by USB VID.

## Installation

```bash
uv tool install hardware-logging   # or: pip install hardware-logging
```

Or run without installing: `uvx --from hardware-logging hwlog ports`

The background daemon and its Unix-socket control channel support macOS and Linux.

## Quick Start

```bash
hwlog ports                     # find your board
hwlog start                     # background capture daemon (auto-detects the board)
hwlog flash -- idf.py flash     # flash through the wrapper (exclusive pause + candidate ELF archive)
hwlog wait --pattern "setup done" --timeout 20   # verify it actually booted
hwlog logs --boot -1 --tail 50  # structured logs from the latest boot
hwlog crashes --last            # full decoded crash artifact, if it crashed
```

### For coding agents

```bash
hwlog init                      # install the agent skill + CLAUDE.md snippet
```

Or add the MCP server (Claude Code shown):

```bash
claude mcp add hardware-logging -- uvx --from hardware-logging hwlog mcp
```

Agents get `query_logs`, `list_boots`, `get_crash`, `wait_for_pattern`, `send_to_device`, `capture_status`. Device telemetry is labeled untrusted, and MCP device writes are disabled unless the user sets `HWLOG_MCP_ALLOW_SEND=1`.

Session data and the local daemon control channel are owner-only. Metadata writes are atomic, selectors cannot escape the session root, and ambiguous multi-board selections require an explicit port.

Storage is bounded by default: a session may retain 512 MiB and the complete
`HWLOG_DIR` may retain 4 GiB. When the session log budget is exhausted, capture
keeps the serial connection alive, stops growing `raw.log`/`log.jsonl`, reserves
space for bounded crash evidence, and records drop counters in `meta.json` and
`hwlog status`. Global cleanup removes only aged, unreferenced archived ELFs and the
oldest completed sessions; active sessions and paths outside the validated
hwlog root are never deleted. Configure byte limits with
`HWLOG_MAX_SESSION_BYTES` and `HWLOG_MAX_TOTAL_BYTES` (zero is fail-closed).

CLI log/boot queries scan the newest 64 MiB by default and warn when older
history was omitted. Override per call with `--scan-bytes` or globally with
`HWLOG_QUERY_SCAN_BYTES`.

## The agent debug loop

1. `hwlog start` — capture runs continuously, owns the port
2. `hwlog flash -- <cmd>` — port-safe flashing, with conservative ELF discovery for symbolization
3. `hwlog wait --pattern <expected>` — behavioral assertion, not compile-and-hope
4. `hwlog logs` / `hwlog crashes --last` — bounded evidence, decoded backtraces
5. Fix firmware, repeat

## Documentation

Full docs in [/docs](./docs): [architecture](./docs/architecture.md) · [CLI reference](./docs/cli.md) · [MCP server](./docs/mcp.md) · [agent workflow](./docs/agent-workflow.md)

## License

MIT
