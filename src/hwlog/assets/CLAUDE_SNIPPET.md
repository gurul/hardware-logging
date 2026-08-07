## Hardware logging (hwlog)

Device serial output is captured by the `hwlog` daemon — never open the serial
port directly and never run blocking monitors (`idf.py monitor`, `screen`).

- Check capture: `hwlog status` (start with `hwlog start` if needed)
- Flash: `hwlog flash -- <your flash command>` (pauses capture, archives ELF)
- Verify after flashing: `hwlog wait --pattern "<expected boot line>" --timeout 20`
- Read logs (bounded): `hwlog logs --boot -1 --tail 50`, `hwlog logs --level E`
- Crashes: `hwlog crashes --last` (decoded backtrace when ELF was archived)
- Reboot-loop check: `hwlog boots`
