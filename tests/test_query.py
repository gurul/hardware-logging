from hwlog import query


def test_filter_by_level_is_min_severity(make_session):
    s = make_session(
        [
            ("E", "app", "boom"),
            ("W", "app", "hmm"),
            ("I", "app", "fyi"),
            ("D", "app", "dbg"),
            (None, None, "freeform"),
        ]
    )
    got = [r.msg for r in query.filter_records(query.iter_records(s), level="W")]
    assert got == ["boom", "hmm"]


def test_grep_is_case_insensitive(make_session):
    s = make_session([("I", "wifi", "Connected to AP"), ("I", "ble", "advertising")])
    got = [r.msg for r in query.filter_records(query.iter_records(s), grep="connected")]
    assert got == ["Connected to AP"]


def test_tag_filter(make_session):
    s = make_session([("I", "wifi", "a"), ("I", "ble", "b")])
    got = [r.msg for r in query.filter_records(query.iter_records(s), tag="ble")]
    assert got == ["b"]


def test_collapse_repeats(make_session):
    s = make_session([("I", "hb", "beat")] * 347 + [("E", "app", "died")])
    collapsed = list(query.collapse_repeats(query.iter_records(s)))
    assert len(collapsed) == 2
    assert collapsed[0].extra["repeat"] == 347
    assert collapsed[1].extra is None or "repeat" not in (collapsed[1].extra or {})


def test_tail_bounds_output(make_session):
    s = make_session([("I", "app", f"line {i}") for i in range(1000)])
    out = query.tail(query.iter_records(s), 50)
    assert len(out) == 50
    assert out[-1].msg == "line 999"


def test_torn_tail_line_is_skipped(make_session):
    s = make_session([("I", "app", "complete")])
    with (s / "log.jsonl").open("a") as f:
        f.write('{"ts": "2026-01-01T00:00:00.000Z", "seq": 99, "bo')  # torn write
    assert [r.msg for r in query.iter_records(s)] == ["complete"]


def test_list_boots_counts(make_session, tmp_path):
    s = make_session([("E", "app", "x"), ("I", "app", "y")])
    rows = query.list_boots(s)
    assert len(rows) == 1
    assert rows[0]["lines"] == 2
    assert rows[0]["errors"] == 1


def test_format_record_compact(make_session):
    s = make_session([("E", "wifi", "connect failed")])
    line = query.format_record(next(iter(query.iter_records(s))))
    assert "E" in line and "wifi:" in line and "connect failed" in line
