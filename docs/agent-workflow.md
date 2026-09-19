# The agent debug loop

How a coding agent uses hwlog to debug firmware recursively — flash, observe, fix, repeat — without a human relaying logs by hand.

## Setup (once per project)

```bash
uv tool install hardware-logging
hwlog init                        # bundled skill + CLAUDE.md snippet
claude mcp add hardware-logging -- uvx --from hardware-logging hwlog mcp   # optional
```

The skill matters as much as the plumbing: it teaches the agent the loop protocol below plus an ESP32 crash-triage playbook (what `LoadProhibited`, task watchdogs, brownouts, and boot loops each mean and what to check first).

## The loop

Discover the interface once with `hwlog describe`; narrow connected boards with `hwlog ports --match ...` or `--vid ... --serial ...`. With MCP, use `capture_status(port=...)` to obtain the selected capture's session path, then pass its final component as the `session` ID to every query. CLI queries accept `--session ID`.

Start observations with `hwlog summary --session ID --json` (MCP: `summarize_session`). It shows firmware provenance, recent faults and whether evidence is missing or truncated. Use the summary's tags and boot index to choose a small detailed query. Inspect explicit scan/storage coverage; zero observed errors is not a successful hardware test.

```
        ┌────────────────────────────────────────────┐
        ▼                                            │
1. hwlog status ──► not running? hwlog start         │
2. edit firmware                                     │
3. hwlog flash -- <build+flash command>              │
4. hwlog wait --pattern "<expected line>" -t 20      │
      │ exit 0: behavior confirmed ──► done ✓        │
      │ exit 1: not seen ──► investigate:            │
5. hwlog boots            (reboot loop?)             │
   hwlog logs --boot -1   (what happened this boot?) │
   hwlog crashes --last   (decoded backtrace)        │
6. form hypothesis from evidence ────────────────────┘
```

## Rules that make it work

- **Never open the serial port directly.** No `idf.py monitor`, no `screen`, no `cat /dev/cu.*`. The daemon owns the port; queries read the recording.
- **Always flash through `hwlog flash -- …`.** Direct esptool against a captured port fails looking exactly like a bricked board — and skipping the wrapper skips ELF archiving, which makes later backtraces undecodable.
- **Gate on behavior, not compilation.** `hwlog wait` exit codes are the loop's success test. "It flashed" is not "it works."
- **Query small, escalate deliberately.** Start at `--boot -1 --tail 50`; escalate to wider windows only when the narrow query didn't answer.
- **Instruments over hypotheses.** One `hwlog crashes --last` with a decoded frame beats an hour of theorizing. When logs look healthy but behavior is wrong, ask the human what they physically see — the user's eyes are an instrument.
- **Keep deterministic control near the device.** Agents read results and revise firmware or scripts. hwlog is an observation interface, not a real-time controller; firmware/drivers must enforce physical limits and interlocks independently of an agent's context.

## Failure modes this design removes

| Classic failure | Why it can't happen here |
|---|---|
| Agent hangs on a blocking monitor | No monitor; queries return immediately |
| Flash fails while monitor holds port | `hwlog flash` pauses capture around the flasher |
| Leaked serial fds wedge the session | Only the daemon holds an fd |
| Crash scrolls away unobserved | Session is persistent; crash artifacts are files |
| Backtrace undecodable after rebuild | ELFs archived per flash, content-hashed |
| Log dump blows the context window | Hard tail ceilings + repeat collapse |
| Silent board misdiagnosed as software | DTR asserted; port events logged; skill playbook covers HWCDC gating |
