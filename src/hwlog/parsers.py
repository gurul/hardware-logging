"""Line parsers: normalize raw device output into structured fields.

Supported formats (auto-detected per line, no configuration):

- ESP-IDF:      ``I (1234) wifi: connected``  (also lines wrapped in ANSI color)
- Arduino core: ``[ 1234][E][WiFiClient.cpp:395] connect(): connect failed``
- Everything else falls through as a freeform line (level/tag None).

Boot detection watches for ESP ROM reset banners so records can be segmented
into boot cycles — "show me logs since the last boot" is the single most
useful agent query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# ESP-IDF: "I (1234) tag: message"  — tag has no colon/space rules beyond "no colon"
ESP_IDF_RE = re.compile(r"^([EWIDV]) \((\d+)\) ([^:]+): (.*)$")

# Arduino esp32 core log_x(): "[  1234][I][file.cpp:42] func(): message"
ARDUINO_RE = re.compile(r"^\[\s*(\d+)\]\[([EWIDV])\]\[([^:\]]+):(\d+)\]\s+(\w+)\(\):\s?(.*)$")

# Reset/boot markers. First-stage ROM output on ESP32-family resets.
BOOT_MARKERS = (
    re.compile(r"^rst:0x[0-9a-fA-F]+"),
    re.compile(r"^ESP-ROM:"),
    re.compile(r"^ets J[au][nl]"),        # older ESP32 ROM date banner ("ets Jun  8 2016")
    re.compile(r"^Rebooting\.\.\."),
)


@dataclass
class ParsedLine:
    msg: str
    level: str | None = None
    tag: str | None = None
    dev_ts: int | None = None
    src: str | None = None
    is_boot_marker: bool = False


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def parse_line(raw: str) -> ParsedLine:
    """Parse one line of device output (without trailing newline)."""
    line = strip_ansi(raw).rstrip("\r\n")

    m = ESP_IDF_RE.match(line)
    if m:
        level, dev_ts, tag, msg = m.groups()
        return ParsedLine(msg=msg, level=level, tag=tag, dev_ts=int(dev_ts))

    m = ARDUINO_RE.match(line)
    if m:
        dev_ts, level, src_file, src_line, func, msg = m.groups()
        return ParsedLine(
            msg=f"{func}(): {msg}" if msg else f"{func}()",
            level=level,
            tag=src_file,
            dev_ts=int(dev_ts),
            src=f"{src_file}:{src_line}",
        )

    is_boot = any(p.search(line) for p in BOOT_MARKERS)
    return ParsedLine(msg=line, is_boot_marker=is_boot)
