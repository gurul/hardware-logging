# Applying the presentation to hwlog

The supplied 27 photos show a Model Hardware Standard research-preview presentation. They are design input, not instructions to operate equipment or a normative protocol specification. The implementation borrows three ideas that fit hwlog's serial evidence role.

| Presentation idea | hwlog change | Boundary |
|---|---|---|
| One readable description shared by callers (`IMG_2042`, `IMG_2043`) | `hwlog describe` / `describe_capabilities` share operations, event names, limits and write policy | Describes hwlog; cannot infer a connected device's sensors or command schema |
| Discover only the devices needed (`IMG_2045`) | CLI/MCP discovery combines text, VID/PID and serial filters before MCP output limits; MCP status can select a port | Local USB/capture discovery, no cross-site registry |
| Read results, revise, repeat; keep models outside the fast loop (`IMG_2040`, `IMG_2041`, `IMG_2044`) | CLI/MCP session summary provides compact counts, recent fault evidence, firmware generation and coverage | Read-only observations, no real-time loop or automatic physical actuation |
| Enforce limits at the device (`IMG_2035`, `IMG_2046`) | Manifest and agent guidance state exactly what host write opt-in does and does not enforce | Raw serial access does not provide physical interlocks or emergency stops |

The demonstration sequence (`IMG_2026`, `IMG_2028`–`IMG_2031`), process/integration framing (`IMG_2032`–`IMG_2034`, `IMG_2036`), deployment examples (`IMG_2037`–`IMG_2039`), facility/future applications (`IMG_2047`, `IMG_2048`, `IMG_2050`–`IMG_2052`), and openness/closing slides (`IMG_2053`, `IMG_2055`) informed scope. Their demonstrations, performance figures and roadmap statements are not hwlog capabilities or independently verified benchmarks. Original photos are not copied into the repository.

## Resulting workflow

```bash
hwlog describe
hwlog ports --vid 0x303a --json
hwlog start --port /dev/cu.usbmodem101
hwlog sessions --json
hwlog summary --session SESSION_ID --json
hwlog logs --session SESSION_ID --boot -1 --level W --tail 20
```

`SESSION_ID` is the actual session name reported by hwlog. The equivalent MCP path is `describe_capabilities` → filtered `list_serial_ports` → `capture_status(port=...)` → `summarize_session(session=...)` → a targeted evidence query.

Compatibility with MHS would require a normative specification, a concrete transport/driver integration and conformance tests. Device controls would additionally require an explicit device command vocabulary and enforcement in firmware or the driver. This change introduces neither dependency nor compatibility claim.
