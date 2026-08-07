from typer.testing import CliRunner

from hwlog.cli import app

runner = CliRunner()


def test_version():
    res = runner.invoke(app, ["version"])
    assert res.exit_code == 0
    assert res.output.strip()


def test_logs_without_session_fails_cleanly():
    res = runner.invoke(app, ["logs"])
    assert res.exit_code == 2


def test_logs_filters_and_tails(make_session):
    make_session([("I", "app", f"line {i}") for i in range(200)] + [("E", "app", "boom")])
    res = runner.invoke(app, ["logs", "--tail", "5"])
    assert res.exit_code == 0
    assert "boom" in res.output
    assert len([ln for ln in res.output.splitlines() if ln.strip()]) <= 5

    res = runner.invoke(app, ["logs", "--level", "E"])
    assert "boom" in res.output and "line 3" not in res.output


def test_logs_json_is_ndjson(make_session):
    make_session([("I", "app", "x")])
    res = runner.invoke(app, ["logs", "--json"])
    assert res.exit_code == 0
    import json

    rec = json.loads(res.output.strip().splitlines()[-1])
    assert rec["msg"] == "x"


def test_boots_and_sessions(make_session):
    make_session([("I", "app", "x")])
    assert runner.invoke(app, ["boots"]).exit_code == 0
    res = runner.invoke(app, ["sessions"])
    assert res.exit_code == 0
    assert "port=/dev/cu.usbmodem101" in res.output


def test_crashes_empty(make_session):
    make_session([("I", "app", "x")])
    res = runner.invoke(app, ["crashes"])
    assert res.exit_code == 0
    assert "no crashes" in res.output


def test_wait_times_out_fast(make_session):
    make_session([("I", "app", "x")])
    res = runner.invoke(app, ["wait", "--pattern", "never-appears", "--timeout", "0.5"])
    assert res.exit_code == 1


def test_wait_matches_existing_with_from_start(make_session):
    make_session([("I", "app", "setup done")])
    res = runner.invoke(
        app, ["wait", "--pattern", "setup done", "--timeout", "1", "--from-start"]
    )
    assert res.exit_code == 0
    assert "setup done" in res.output


def test_status_no_daemon():
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0
    assert "no daemon" in res.output
