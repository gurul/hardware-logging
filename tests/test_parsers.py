import pytest

from hwlog.parsers import parse_line, strip_ansi
from hwlog.records import (
    MAX_COUNTER,
    MAX_MESSAGE_CHARS,
    MAX_SRC_CHARS,
    MAX_TAG_CHARS,
    LogRecord,
)


def test_esp_idf_line():
    p = parse_line("I (1234) wifi: connected to AP")
    assert (p.level, p.tag, p.dev_ts, p.msg) == ("I", "wifi", 1234, "connected to AP")


def test_esp_idf_line_with_ansi_color():
    p = parse_line("\x1b[0;31mE (99) rmt: tx failed\x1b[0m")
    assert (p.level, p.tag, p.msg) == ("E", "rmt", "tx failed")


def test_arduino_core_line():
    p = parse_line("[  5678][E][WiFiClient.cpp:395] connect(): connect failed")
    assert p.level == "E"
    assert p.src == "WiFiClient.cpp:395"
    assert p.dev_ts == 5678
    assert "connect failed" in p.msg


def test_freeform_line_passes_through():
    p = parse_line("hello from printf")
    assert p.level is None
    assert p.msg == "hello from printf"


def test_boot_markers():
    assert parse_line("rst:0x1 (POWERON_RESET),boot:0x13").is_boot_marker
    assert parse_line("ESP-ROM:esp32s3-20210327").is_boot_marker
    assert parse_line("ets Jun  8 2016 00:22:57").is_boot_marker
    assert parse_line("Rebooting...").is_boot_marker
    assert not parse_line("I (1) main: normal line").is_boot_marker


def test_strip_ansi():
    assert strip_ansi("\x1b[32mgreen\x1b[0m plain") == "green plain"


def test_crlf_stripped():
    assert parse_line("I (1) a: b\r").msg == "b"


@pytest.mark.parametrize(
    "raw",
    [
        f"I ({'9' * 5000}) app: hostile",
        f"I ({MAX_COUNTER + 1}) app: hostile",
        "I (not-a-number) app: hostile",
        f"[{'9' * 5000}][I][app.cpp:1] loop(): hostile",
        f"[{MAX_COUNTER + 1}][I][app.cpp:1] loop(): hostile",
        f"[1][I][app.cpp:{'9' * 5000}] loop(): hostile",
        f"[1][I][app.cpp:{MAX_COUNTER + 1}] loop(): hostile",
        "[1][I][app.cpp:not-a-number] loop(): hostile",
    ],
)
def test_invalid_numeric_fields_fall_back_to_freeform(raw):
    parsed = parse_line(raw)

    assert parsed.msg == raw
    assert parsed.level is None
    assert parsed.dev_ts is None
    assert parsed.src is None


def test_maximum_numeric_fields_remain_structured():
    esp = parse_line(f"I ({MAX_COUNTER}) app: ok")
    arduino = parse_line(f"[{MAX_COUNTER}][I][app.cpp:{MAX_COUNTER}] loop(): ok")

    assert (esp.level, esp.dev_ts) == ("I", MAX_COUNTER)
    assert (arduino.level, arduino.dev_ts) == ("I", MAX_COUNTER)
    assert arduino.src == f"app.cpp:{MAX_COUNTER}"


def test_parser_outputs_fit_log_record_schema():
    esp = parse_line(f"I (1) {'t' * (MAX_TAG_CHARS + 100)}: {'m' * (MAX_MESSAGE_CHARS + 100)}")
    arduino = parse_line(f"[1][I][{'f' * (MAX_SRC_CHARS + 100)}:42] loop(): ok")

    assert len(esp.tag) == MAX_TAG_CHARS
    assert len(esp.msg) == MAX_MESSAGE_CHARS
    assert len(arduino.tag) == MAX_TAG_CHARS
    assert len(arduino.src) == MAX_SRC_CHARS
    assert arduino.src.endswith(":42")

    for parsed in (esp, arduino):
        record = LogRecord(
            ts="2026-08-07T00:00:00Z",
            seq=1,
            boot=0,
            level=parsed.level,
            tag=parsed.tag,
            msg=parsed.msg,
            dev_ts=parsed.dev_ts,
            src=parsed.src,
        )
        assert LogRecord.from_json(record.to_json()) == record
