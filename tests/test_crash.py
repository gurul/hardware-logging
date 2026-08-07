from hwlog.crash import CrashAssembler, decode_backtrace, is_crash_line

PANIC = "Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled."
BACKTRACE = "Backtrace: 0x400d1234:0x3ffb1234 0x400d5678:0x3ffb5678 0x400dabcd:0x3ffb9abc"


def test_crash_signatures():
    for line in [
        PANIC,
        "abort() was called at PC 0x400d1234",
        "assert failed: do_thing main.c:42",
        "Task watchdog got triggered. The following tasks did not reset the watchdog in time:",
        "CORRUPT HEAP: Bad head at 0x3ffb1234",
        "Brownout detector was triggered",
    ]:
        assert is_crash_line(line), line
    assert not is_crash_line("I (1) main: all good")


def test_assembler_full_artifact():
    ca = CrashAssembler()
    assert ca.feed(PANIC) is None
    assert ca.in_crash
    assert ca.feed("Core  1 register dump:") is None
    assert ca.feed(BACKTRACE) is None
    report = ca.feed("Rebooting...")
    assert report is not None
    assert report.crash_id == 1
    assert report.first_line == PANIC
    assert report.backtrace_addrs == ["0x400d1234", "0x400d5678", "0x400dabcd"]
    assert not ca.in_crash


def test_assembler_flush_on_port_loss():
    ca = CrashAssembler()
    ca.feed(PANIC)
    ca.feed(BACKTRACE)
    report = ca.flush()
    assert report is not None
    assert len(report.backtrace_addrs) == 3


def test_assembler_line_budget():
    ca = CrashAssembler()
    ca.feed(PANIC)
    report = None
    for i in range(300):
        report = ca.feed(f"noise {i}")
        if report:
            break
    assert report is not None  # bounded even without an end marker


def test_two_crashes_back_to_back():
    ca = CrashAssembler()
    ca.feed(PANIC)
    ca.feed(BACKTRACE)
    first = ca.feed("abort() was called at PC 0x40000000")  # new start ends previous
    assert first is not None and first.crash_id == 1
    assert ca.in_crash
    second = ca.feed("Rebooting...")
    assert second is not None and second.crash_id == 2


def test_decode_without_tool_or_addrs_is_empty():
    assert decode_backtrace([], "/nonexistent.elf") == []
    # Missing ELF: whether or not an addr2line binary exists on this host,
    # decoding must fail soft (empty), never raise.
    assert decode_backtrace(["0x400d1234"], "/nonexistent.elf") == []
