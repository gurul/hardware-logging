# CLI reference

All query commands print compact text by default and NDJSON with `--json`. Exit codes are part of the contract.

## Capture

### `hwlog ports [--json]`
List serial ports, likely dev boards first (identified by USB VID: Espressif, CP210x, CH340, FTDI, RP2040, STM32, nRF, …).

### `hwlog start [--port SPEC] [--baud N]`
Start the background capture daemon. `--port` accepts an exact path or a substring (`usbmodem` survives replug renumbering); omitted, the best board candidate is auto-detected. Idempotent — returns the existing daemon if one is running.

After upgrading from a release with the older unauthenticated daemon protocol, live legacy state is preserved and new capture refuses to start alongside it. Stop that process with the previous `hwlog` version, or verify the recorded PID before terminating it manually.

### `hwlog monitor [--port SPEC] [--baud N]`
Foreground capture with live output. Records a full session exactly like the daemon. Ctrl-C to stop.

### `hwlog stop [--port SPEC]` / `hwlog status [--json]`
Stop the daemon / show daemon + session status. Status includes the configured
session storage limit and persisted raw-byte, structured-record, and crash drop
counters if capture reached a storage cap.

## Query

### `hwlog logs [options]`
Bounded log query over the current (or `--session`) session.

| Option | Meaning |
|---|---|
| `--tail/-n N` | max lines returned (default 100; hard ceiling 500) |
| `--boot N` | only boot cycle N; `-1` = latest boot |
| `--level/-l L` | min severity — `E` errors only, `W` = E+W, then `I`, `D`, `V` |
| `--grep/-g STR` | case-insensitive substring on message |
| `--tag STR` | exact tag match |
| `--since ISO` | timestamp lower bound |
| `--no-collapse` | keep repeated lines instead of `msg (×N)` |
| `--json` | NDJSON records |
| `--scan-bytes N` | inspect at most the newest N bytes (default 64 MiB, or `HWLOG_QUERY_SCAN_BYTES`) |

When the scan window omits older data, the command prints a warning on stderr;
an empty result then means "no match in the scanned window," not necessarily the
entire session.

### `hwlog boots [--json] [--scan-bytes N]`
One row per boot cycle in the bounded newest-byte window: start time,
line/error/crash counts. Many short boots = boot loop. Output is capped at the
newest 500 boot rows.

### `hwlog crashes [--last] [--json]`
List up to the newest 500 crash summaries. With `--json`, the list is summary NDJSON rather than repeated full artifacts. `--last` prints the one most recent full artifact — raw panic lines plus the `addr2line`-decoded backtrace when an ELF was archived.

### `hwlog sessions [--json]`
All retained sessions, oldest first. Capped sessions include their reason and
drop counters.

## Storage limits

`HWLOG_MAX_SESSION_BYTES` defaults to 536870912 (512 MiB) and
`HWLOG_MAX_TOTAL_BYTES` defaults to 4294967296 (4 GiB). Values are integer
bytes; zero disables further data growth. A small part of the session budget is
reserved for bounded crash artifacts. On the global limit, hwlog first removes
aged unreferenced files directly inside its ELF archive, then the oldest completed
sessions. It never prunes active sessions or follows symlinks outside
`HWLOG_DIR`; a newly archived ELF receives a grace period so concurrent
registration cannot be mistaken for stale data.

## Act

### `hwlog flash [--port SPEC] -- <command...>`
Run any flash command under an exclusive pause lease, then conservatively discover and archive at most one ELF candidate for backtrace symbolization. Multiple concurrent wrappers are rejected. Every attempted command invalidates the previous firmware/ELF association, including a nonzero exit because the device may have been partially programmed. A successful ESP-IDF or PlatformIO upload may reuse an unchanged ELF only when its content exactly matches the last archive; otherwise, if no candidate can be identified without ambiguity, the new generation remains unassociated and crashes stay undecoded. Generic flash commands do not expose which binary they programmed, so automatic ELF discovery is best-effort provenance rather than proof that the selected artifact reached the device. Exit code = the flash command's exit code.

Capture resumes when the wrapped process exits, with the target daemon resumed before unrelated captures. Output emitted while the flash tool still owns the port—including firmware output during tool-side cleanup before exit—cannot be recorded.

```bash
hwlog flash -- idf.py -p /dev/cu.usbmodem101 flash
hwlog flash -- arduino-cli upload --fqbn esp32:esp32:esp32s3 .
hwlog flash -- pio run -t upload
```

### `hwlog wait --pattern REGEX [--timeout S] [--from-start]`
Block until the pattern appears in device output. Exit 0 = matched (record printed), 1 = timeout, 2 = invalid input/no session. By default a post-flash wait starts at the latest flash boundary, so output captured after resume but before `wait` starts still counts; without a boundary it starts at the invocation-time end of the log. `--from-start` scans the existing session too. Patterns are limited to 512 characters, each match has an execution timeout, and waits are capped at 120 seconds.

### `hwlog send TEXT [--no-newline]`
Send a line to the device through the daemon — stimulus injection for firmware with a debug command handler. Payloads are capped at 4096 bytes; the host-generated `sent` record stores only the byte count. Firmware or terminal echo is device telemetry and remains in `log.jsonl` and `raw.log`, so do not send secrets to echoing firmware.

## Agent setup

### `hwlog mcp`
Run the MCP server on stdio. See [mcp.md](./mcp.md).

### `hwlog init [--path DIR]`
Install the bundled agent skill to `.claude/skills/hardware-logging/SKILL.md` and print a CLAUDE.md snippet.
