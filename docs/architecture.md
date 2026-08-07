# Architecture

## Design principle: decouple capture from consumption

The single architectural decision everything else follows from: **one daemon owns the serial port; every consumer reads files.**

Serial ports are single-owner resources. Every classic failure in agent-driven hardware work — blocking monitors hanging the agent, esptool failing against a port a monitor holds (which looks exactly like a bricked board), leaked serial file descriptors wedging the whole session — comes from multiple processes fighting over the port. So `hwlog` never lets that fight happen:

- The **capture daemon** (`hwlog start`, or foreground `hwlog monitor`) opens the port exclusively and writes everything to a session directory.
- **Queries** (`hwlog logs`, `boots`, `crashes`, `sessions`, and all MCP read tools) only read session files. They work while capture runs, after it stops, and from any number of processes at once.
- **Control operations** that genuinely need the port (send-to-device, pause/resume around flashing, stop) go through a Unix-socket control channel to the daemon.

## Session storage

```
$HWLOG_DIR (default ~/.hwlog)/
├── sessions/<YYYYmmdd-HHMMSS>-<port-slug>/
│   ├── meta.json      # port, board hints, archived ELFs, boot/crash counts
│   ├── log.jsonl      # one structured record per line — the query surface
│   ├── raw.log        # verbatim bytes — the forensic surface
│   └── crashes/NNN.json
├── current -> sessions/<latest>
├── daemon/<port-slug>.{json,sock,out}   # daemon state + control socket
└── elf-archive/                          # ELFs archived at flash time
```

Records are JSONL with a small flat schema: `ts` (host time), `seq`, `boot` (boot-cycle index), `event` (`log` / `boot` / `crash` / `status` / `sent`), `level`, `tag`, `msg`, `dev_ts` (device ms), `src`, `extra`. Torn tail lines (a write in progress) are skipped by readers.

Keeping both `log.jsonl` and `raw.log` is deliberate: structure serves the agent's token budget, raw bytes serve forensics when the parser turns out to be wrong.

## The capture loop

`capture.py` encodes serial behaviors that each trace to a real field incident:

| Behavior | Incident it prevents |
|---|---|
| DTR asserted at open | ESP32-S3 native USB (HWCDC) drops all TX until the host asserts DTR — `cat /dev/cu.X` shows nothing and misdirects debugging |
| Exclusive open | Two readers corrupt both streams |
| Port re-resolution on every reconnect | USB re-enumeration renumbers ports (`usbmodem101` → `usbmodem1101`) |
| Reopen backoff capped at 15s | Instant reopen after re-enumeration lands on a half-dead fd |
| Status records for every port event | A 52-minute silent hole in a log is how you lose an afternoon |
| Boot-marker debounce (3s) | ROM emits several reset banner lines per boot; count one boot |

## Crash pipeline

1. **Detect** — a signature regex (Guru Meditation, watchdogs, `abort()`, heap corruption, brownout…) flags the start line.
2. **Assemble** — subsequent lines (register dump, `Backtrace:`) are collected into one artifact, ended by the reboot marker or a line budget. Port loss mid-crash flushes the partial artifact instead of dropping it.
3. **Decode** — backtrace program counters are symbolized with `addr2line` (ESP toolchain binaries preferred) against the newest archived ELF.

ELF archiving happens in `hwlog flash`: a backtrace is only symbolizable against the exact build that produced it, so the wrapper archives any fresh ELF (content-hashed) on every flash. Keep flashing through the wrapper and old crashes stay decodable even after you rebuild.

## Bounded queries

Agents have token budgets. Every query path enforces: tail limits (hard ceiling 500 in MCP), level/tag/grep filters, boot-cycle scoping, and collapse of consecutive identical lines into `msg (×347)`. The flood case (a heartbeat or error spamming thousands of lines) costs a handful of tokens instead of a context window.

## What hwlog is not

- Not a flasher — it wraps yours (`idf.py`, `arduino-cli`, `pio`, raw `esptool`).
- Not a firmware library — nothing to compile in; it consumes whatever your board already prints.
- Not a dashboard — sessions are plain JSONL; point `jq`, a notebook, or a UI at them.
