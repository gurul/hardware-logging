# hardware-logging

![hardware-logging — structured, crash-aware serial logging](https://raw.githubusercontent.com/gurul/hardware-logging/main/docs/assets/hero.png)

Structured, crash-aware serial logging for embedded boards — built so AI coding agents can debug firmware recursively.

`hwlog` runs a small daemon that owns your board's serial port and records everything to structured sessions on disk. Your coding agent (Claude Code, Cursor, anything) never touches the port — it queries the recording through bounded CLI commands or native MCP tools, flashes through a port-safe wrapper, and verifies behavior instead of assuming "it compiled" means "it works."

## Why

Wiring a coding agent to a dev board fails in predictable ways: blocking monitors hang the agent, flashing fights the monitor for the port (and looks exactly like a bricked board), ESP32-S3 native USB drops all output until DTR is asserted, ports renumber on replug, crashes scroll away before anyone reads them, and a raw log dump blows the agent's context window. `hwlog` packages the fixes — learned from real hardware incidents — into one tool.

## Features

- **Discover before operating** — `hwlog describe` publishes a versioned capability manifest; `hwlog ports --match esp32 --vid 0x303a` narrows device discovery before MCP response limits
- **Compact evidence summaries** — `hwlog summary` reports observed faults, firmware generation, storage loss and scan coverage before an agent requests detailed logs
- **Persistent capture sessions** — logs are recorded to disk continuously; the crash that happened while your agent was thinking is still there
- **Structure at ingest** — ANSI stripped; ESP-IDF and Arduino log formats parsed into `{level, tag, msg, timestamp}`
- **Boot-cycle segmentation** — "logs since the last boot" is one flag (`--boot -1`); reboot loops are instantly visible in `hwlog boots`
- **Crash reports, assembled and decoded** — panics/watchdogs/heap corruption are captured as complete multi-line artifacts and symbolized with `addr2line` against ELFs archived at flash time — source lines, not addresses
- **Bounded, agent-budget-aware queries** — line, byte, scan, regex-runtime, and timeout ceilings plus repeated-line collapse (`heartbeat (×347)`)
- **Flash-safe port arbitration** — `hwlog flash -- <cmd>` holds an exclusive pause lease, archives a generation-bound ELF for symbolization, and resumes when the tool exits
- **Behavioral verification** — `hwlog wait --pattern "setup done" --timeout 20` asserts against output since the latest flash, with CI-friendly exit codes
- **MCP server + bundled agent skill** — `hwlog mcp` exposes everything as native agent tools; `hwlog init` installs a debug playbook into your project

Works with anything that talks serial: ESP32 family first-class, plus RP2040, STM32, nRF, Arduino — identified by USB VID. The daemon supports macOS and Linux.

## Installation

```bash
uv tool install hardware-logging   # or: pip install hardware-logging
```

Or run without installing: `uvx --from hardware-logging hwlog ports`

## Quick Start

```bash
hwlog ports                     # find your board
hwlog describe                  # JSON capabilities, limits and current MCP write policy
hwlog start                     # background capture daemon (auto-detects the board)
hwlog flash -- idf.py flash     # flash through the wrapper (exclusive pause + ELF archive)
hwlog wait --pattern "setup done" --timeout 20   # verify it actually booted
hwlog summary                   # compact evidence and coverage from the recorded session
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

Agents get `describe_capabilities`, filtered `list_serial_ports`, `summarize_session`, `query_logs`, `list_boots`, `get_crash`, `wait_for_pattern`, `send_to_device`, and `capture_status(port=...)`. Device telemetry is labeled untrusted, and MCP device writes are disabled unless the user sets `HWLOG_MCP_ALLOW_SEND=1`. Session data and the daemon control channel are owner-only; storage and query scans are bounded by default — see [storage limits](./docs/cli.md#storage-limits).

The [MHS-inspired design notes](./docs/mhs-design-notes.md) explain the discovery and observation workflow. hwlog remains a serial evidence tool: it does not implement MHS, infer a device's command schema, or enforce physical safety limits.

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
