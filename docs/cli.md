# CLI reference

All query commands print compact text by default and NDJSON with `--json`. Exit codes are part of the contract.

## Capture

### `hwlog ports [--json]`
List serial ports, likely dev boards first (identified by USB VID: Espressif, CP210x, CH340, FTDI, RP2040, STM32, nRF, …).

### `hwlog start [--port SPEC] [--baud N]`
Start the background capture daemon. `--port` accepts an exact path or a substring (`usbmodem` survives replug renumbering); omitted, the best board candidate is auto-detected. Idempotent — returns the existing daemon if one is running.

### `hwlog monitor [--port SPEC] [--baud N]`
Foreground capture with live output. Records a full session exactly like the daemon. Ctrl-C to stop.

### `hwlog stop [--port SPEC]` / `hwlog status [--json]`
Stop the daemon / show daemon + session status.

## Query

### `hwlog logs [options]`
Bounded log query over the current (or `--session`) session.

| Option | Meaning |
|---|---|
| `--tail/-n N` | max lines returned (default 100) |
| `--boot N` | only boot cycle N; `-1` = latest boot |
| `--level/-l L` | min severity — `E` errors only, `W` = E+W, then `I`, `D`, `V` |
| `--grep/-g STR` | case-insensitive substring on message |
| `--tag STR` | exact tag match |
| `--since ISO` | timestamp lower bound |
| `--no-collapse` | keep repeated lines instead of `msg (×N)` |
| `--json` | NDJSON records |

### `hwlog boots [--json]`
One row per boot cycle: start time, line/error/crash counts. Many short boots = boot loop.

### `hwlog crashes [--last] [--json]`
List crash reports; `--last` prints the full artifact — raw panic lines plus the `addr2line`-decoded backtrace when an ELF was archived.

### `hwlog sessions [--json]`
All recorded sessions, oldest first.

## Act

### `hwlog flash [--port SPEC] -- <command...>`
Run any flash command with capture paused around it, then archive the freshest ELF for backtrace symbolization. Exit code = the flash command's exit code.

```bash
hwlog flash -- idf.py -p /dev/cu.usbmodem101 flash
hwlog flash -- arduino-cli upload --fqbn esp32:esp32:esp32s3 .
hwlog flash -- pio run -t upload
```

### `hwlog wait --pattern REGEX [--timeout S] [--from-start]`
Block until the pattern appears in device output. Exit 0 = matched (record printed), 1 = timeout, 2 = no session. By default only NEW output counts; `--from-start` scans the existing session too.

### `hwlog send TEXT [--no-newline]`
Send a line to the device through the daemon — stimulus injection for firmware with a debug command handler.

## Agent setup

### `hwlog mcp`
Run the MCP server on stdio. See [mcp.md](./mcp.md).

### `hwlog init [--path DIR]`
Install the bundled agent skill to `.claude/skills/hardware-logging/SKILL.md` and print a CLAUDE.md snippet.
