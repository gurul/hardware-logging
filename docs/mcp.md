# MCP server

`hwlog mcp` runs a stdio MCP server exposing the query surface as native agent tools. Registration (Claude Code):

```bash
claude mcp add hardware-logging -- uvx --from hardware-logging hwlog mcp
```

Or in any MCP client config:

```json
{
  "mcpServers": {
    "hardware-logging": {
      "command": "uvx",
      "args": ["--from", "hardware-logging", "hwlog", "mcp"]
    }
  }
}
```

Capture still runs separately (`hwlog start`) — the MCP server reads session files and only talks to the daemon for `send_to_device` and status.

## Tools

| Tool | Purpose | Bounds |
|---|---|---|
| `query_logs(tail, boot, level, grep, tag, collapse_repeats)` | Structured log query; `boot=-1` = latest boot | hard ceiling 500 lines; repeat collapse on by default |
| `list_boots()` | Boot cycles with line/error/crash counts — the reboot-loop detector | one row per boot |
| `get_crash(crash_id?)` | Full crash artifact, decoded backtrace when available; defaults to latest | single artifact, line-budgeted at capture |
| `wait_for_pattern(pattern, timeout_s)` | Block until regex appears in NEW output — behavioral verification | timeout capped at 120s |
| `send_to_device(text)` | Stimulus injection over serial | one line |
| `capture_status()` | Daemon running? connected? which session? | — |
| `list_serial_ports()` | Board discovery with VID identification | — |
| `list_capture_sessions()` | Session history | — |

## Design contract

Every tool is bounded — no tool can dump an unbounded stream into a context window. Reads never touch the serial port, so tools are safe to call at any frequency, during flashing, or with no device attached (they fail soft with a hint instead of raising).
