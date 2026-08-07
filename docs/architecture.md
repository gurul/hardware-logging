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
├── daemon/<port-slug>.{json,out,lock}   # daemon state/output/startup lock
└── elf-archive/                          # ELFs archived at flash time
```

The owner-only control socket lives under a short, per-user directory in the
system temporary directory because Unix-domain socket paths are sharply limited
on macOS and Linux. Its name is derived from `$HWLOG_DIR` and the port selector.

Records are JSONL with a small flat schema: `ts` (host time), `seq`, `boot` (boot-cycle index), `event` (`log` / `boot` / `crash` / `status` / `sent`), `level`, `tag`, `msg`, `dev_ts` (device ms), `src`, `extra`. Torn tail lines (a write in progress) are skipped by readers.

Keeping both `log.jsonl` and `raw.log` is deliberate: structure serves the agent's token budget, raw bytes serve forensics when the parser turns out to be wrong.

Storage directories are mode `0700`; logs, metadata, daemon state, archived ELFs,
and control sockets are mode `0600`. Session IDs are validated as direct children
of `sessions/`, directories are allocated exclusively, and JSON state is published
with atomic replacement so readers never observe a truncated update. File data
and the containing directory are fsynced where supported, making the rename
power-loss durable as well as atomic.

Capture data has two default byte boundaries: 512 MiB per session and 4 GiB for
the full hwlog root. The limits are configurable with
`HWLOG_MAX_SESSION_BYTES` and `HWLOG_MAX_TOTAL_BYTES`. The session writer
reserves bounded crash capacity; once ordinary logs reach their allowance it
keeps the port alive but drops further raw/structured writes and checkpoints
counters in metadata. Global pruning considers only regular files below the
owner-controlled root, deletes aged unreferenced direct-child ELF archives first,
and then removes oldest completed sessions. Active sessions, corrupt/uncertain
references, symlinks, newly archived in-flight ELFs, and anything outside those
validated roots fail closed.

The control protocol uses a per-daemon instance token and bounded messages. Live
state from the older unauthenticated protocol is preserved during upgrades but is
never sent a mutating command; new capture fails closed until that process is
stopped and its ownership is verified.

## The capture loop

`capture.py` encodes serial behaviors that each trace to a real field incident:

| Behavior | Incident it prevents |
|---|---|
| DTR asserted at open | ESP32-S3 native USB (HWCDC) drops all TX until the host asserts DTR — `cat /dev/cu.X` shows nothing and misdirects debugging |
| Exclusive open | Two readers corrupt both streams |
| Port re-resolution on every reconnect | USB re-enumeration renumbers ports (`usbmodem101` → `usbmodem1101`) |
| Reopen backoff capped at 15s | Instant reopen after re-enumeration lands on a half-dead fd |
| Status records for every port event | A 52-minute silent hole in a log is how you lose an afternoon |
| Boot-marker debounce (3s from the last accepted marker) | ROM emits several reset banner lines per boot without hiding a fast reboot loop |
| Physical USB identity pinning | A reconnect cannot silently switch a session to a different serialized board |
| Acknowledged, owned pause lease | A flasher starts only after the serial descriptor is closed; competing flashers are rejected and abandoned leases recover when their owner exits |
| Physical-device ownership lock | Auto and explicit selectors cannot run two hwlog owners against the same serialized board, including while capture is paused |

## Crash pipeline

1. **Detect** — a signature regex (Guru Meditation, watchdogs, `abort()`, heap corruption, brownout…) flags the start line.
2. **Assemble** — subsequent lines (register dump, `Backtrace:`) are collected into one artifact, ended by the reboot marker or a line budget. Port loss mid-crash flushes the partial artifact instead of dropping it.
3. **Decode** — backtrace program counters are symbolized in a background worker with `addr2line` (ESP toolchain binaries preferred) against the archived ELF for that exact firmware generation, so capture does not stall while the device reboots. An unresolved crash is never carried forward to a later generation.

ELF archiving happens in `hwlog flash`: a backtrace is only symbolizable against the exact build that produced it, so every flash attempt retires the prior association. The wrapper archives a content-hashed candidate only when one changed artifact can be identified unambiguously, or when a known upload tool has one unchanged candidate whose content matches the prior archive. Registration carries the firmware generation, preventing delayed work from attaching stale symbols to a later flash. This is conservative, best-effort provenance: a generic flasher does not report which artifact it actually programmed, so an ambiguous build leaves symbolization pending instead of guessing.

The post-flash log offset is persisted before capture resumes. A following `wait` therefore includes startup output recorded before the wait command begins. The generic wrapper cannot capture output emitted before the flash process exits while that process still owns the serial port.

## Bounded queries

Agents have token budgets. Query paths enforce a 500-record ceiling, MCP
per-record/total-output and 16 MiB scan budgets, and a 64 MiB default CLI scan
budget (`--scan-bytes` / `HWLOG_QUERY_SCAN_BYTES`). A CLI warning explicitly
marks partial-history results. Level/tag/grep filters, boot-cycle scoping, and
collapse of consecutive identical lines into `msg (×347)` keep the flood case
(a heartbeat or error spamming thousands of lines) to a handful of tokens.
Regex searches use a bounded pattern length and per-record execution timeout.

Structured text is terminal-sanitized; the exact device bytes remain available only in `raw.log`. Device output is untrusted telemetry and must never be treated as instructions or authorization by an agent.

## What hwlog is not

- Not a flasher — it wraps yours (`idf.py`, `arduino-cli`, `pio`, raw `esptool`).
- Not a firmware library — nothing to compile in; it consumes whatever your board already prints.
- Not a dashboard — sessions are plain JSONL; point `jq`, a notebook, or a UI at them.
