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
import unicodedata
from dataclasses import dataclass

from .records import MAX_COUNTER, MAX_MESSAGE_CHARS, MAX_SRC_CHARS, MAX_TAG_CHARS

# Terminal control sequences are untrusted input. Preserve the original bytes in
# raw.log, but remove CSI/OSC/DCS and single-character ESC sequences before a
# record can reach a terminal, JSON consumer, or agent.
ANSI_RE = re.compile(
    r"(?:"
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC (including OSC-52 clipboard)
    r"|\x1b[P^_X][^\x1b]*(?:\x1b\\)?"  # DCS/SOS/PM/APC strings
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"  # CSI, including private modes
    r"|\x1b[@-_]"  # two-byte ESC sequences
    r")"
)

# Formatting controls such as bidi overrides are invisible but can spoof the
# visual order of logs. Surrogates cannot be encoded to UTF-8. Keep ordinary
# Unicode text and tabs, normalize Unicode line separators, and remove the
# remaining Unicode "Other" code points before records reach a renderer.
_ALLOWED_CONTROLS = {"\t"}
_LINE_SEPARATORS = {"\u2028", "\u2029"}

_COUNTER_PATTERN = rf"[0-9]{{1,{len(str(MAX_COUNTER))}}}"

# ESP-IDF: "I (1234) tag: message"  — tag has no colon/space rules beyond "no colon"
ESP_IDF_RE = re.compile(rf"^([EWIDV]) \(({_COUNTER_PATTERN})\) ([^:]+): (.*)$")

# Arduino esp32 core log_x(): "[  1234][I][file.cpp:42] func(): message"
ARDUINO_RE = re.compile(
    rf"^\[\s*({_COUNTER_PATTERN})\]\[([EWIDV])\]"
    rf"\[([^:\]]+):({_COUNTER_PATTERN})\]\s+(\w+)\(\):\s?(.*)$"
)

# Reset/boot markers. First-stage ROM output on ESP32-family resets.
BOOT_MARKERS = (
    re.compile(r"^rst:0x[0-9a-fA-F]+"),
    re.compile(r"^ESP-ROM:"),
    re.compile(r"^ets J[au][nl]"),  # older ESP32 ROM date banner ("ets Jun  8 2016")
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
    text = ANSI_RE.sub("", text)
    cleaned = []
    for char in text:
        if char in _LINE_SEPARATORS:
            cleaned.append(" ")
        elif char in _ALLOWED_CONTROLS or not unicodedata.category(char).startswith("C"):
            cleaned.append(char)
    return "".join(cleaned)


def _parse_counter(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if 0 <= parsed <= MAX_COUNTER else None


def _freeform(line: str) -> ParsedLine:
    return ParsedLine(
        msg=line[:MAX_MESSAGE_CHARS],
        is_boot_marker=any(pattern.search(line) for pattern in BOOT_MARKERS),
    )


def parse_line(raw: str) -> ParsedLine:
    """Parse one line of device output (without trailing newline)."""
    line = strip_ansi(raw).rstrip("\r\n")

    m = ESP_IDF_RE.match(line)
    if m:
        level, dev_ts, tag, msg = m.groups()
        parsed_ts = _parse_counter(dev_ts)
        if parsed_ts is None:
            return _freeform(line)
        return ParsedLine(
            msg=msg[:MAX_MESSAGE_CHARS],
            level=level,
            tag=tag[:MAX_TAG_CHARS],
            dev_ts=parsed_ts,
        )

    m = ARDUINO_RE.match(line)
    if m:
        dev_ts, level, src_file, src_line, func, msg = m.groups()
        parsed_ts = _parse_counter(dev_ts)
        if parsed_ts is None or _parse_counter(src_line) is None:
            return _freeform(line)
        rendered_msg = f"{func}(): {msg}" if msg else f"{func}()"
        src_suffix = f":{src_line}"
        bounded_src_file = src_file[: MAX_SRC_CHARS - len(src_suffix)]
        return ParsedLine(
            msg=rendered_msg[:MAX_MESSAGE_CHARS],
            level=level,
            tag=src_file[:MAX_TAG_CHARS],
            dev_ts=parsed_ts,
            src=f"{bounded_src_file}{src_suffix}",
        )

    return _freeform(line)
