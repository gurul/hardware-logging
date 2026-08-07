from hwlog.parsers import parse_line, strip_ansi


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
