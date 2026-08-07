from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hwlog import capture, daemon


@pytest.fixture
def running_daemon(monkeypatch):
    """Run the real control server while guaranteeing that no serial port is opened."""
    short_home = tempfile.TemporaryDirectory(prefix="hwd-", dir="/tmp")
    monkeypatch.setenv("HWLOG_DIR", short_home.name)
    loops: list[capture.CaptureLoop] = []
    errors: list[BaseException] = []
    original_loop = daemon.CaptureLoop

    class TrackedCaptureLoop(original_loop):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            loops.append(self)

    monkeypatch.setattr(daemon, "CaptureLoop", TrackedCaptureLoop)
    monkeypatch.setattr(capture.ports, "resolve_port", lambda _spec=None: None)
    monkeypatch.setattr(
        daemon,
        "signal",
        SimpleNamespace(
            SIGTERM=signal.SIGTERM,
            SIGINT=signal.SIGINT,
            signal=lambda *_args, **_kwargs: None,
        ),
    )

    port = "missing"
    state_file = daemon.state_path(daemon.port_slug(port))

    def run() -> None:
        try:
            daemon.run_daemon(port, 115200)
        except BaseException as exc:  # make thread failures visible to the test
            errors.append(exc)

    thread = threading.Thread(target=run, name="test-hwlog-daemon")
    thread.start()

    deadline = time.monotonic() + 5
    while not state_file.exists():
        if errors:
            raise errors[0]
        if time.monotonic() >= deadline:
            pytest.fail("daemon state file was not created")
        time.sleep(0.01)
    state = json.loads(state_file.read_text(encoding="utf-8"))

    try:
        yield state, state_file, thread
    finally:
        if thread.is_alive():
            with contextlib.suppress(OSError, daemon.DaemonError):
                daemon.send_cmd(state, {"cmd": "stop"}, timeout=1)
        # A broken control thread must not leave the capture thread behind.
        if thread.is_alive() and loops:
            loops[0].stop()
        thread.join(timeout=3)
        assert not thread.is_alive(), "test daemon failed to exit"
        if errors:
            raise errors[0]
        short_home.cleanup()


def _raw_request(sock_path: str, payload: bytes) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(2)
        conn.connect(sock_path)
        conn.sendall(payload)
        response = bytearray()
        while b"\n" not in response:
            chunk = conn.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
    assert response, "control server closed without a response"
    return json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))


@pytest.mark.parametrize("pid", [None, "123", True, False, -1, 0, 1])
def test_invalid_pids_never_reach_control_or_os_kill(monkeypatch, pid):
    kill_calls = []
    send_calls = []

    def fake_kill(*args):
        kill_calls.append(args)
        raise ProcessLookupError

    def fake_send(*args, **kwargs):
        send_calls.append((args, kwargs))
        raise OSError("control channel unavailable")

    monkeypatch.setattr(daemon.os, "kill", fake_kill)
    monkeypatch.setattr(daemon, "send_cmd", fake_send)

    assert daemon._pid_alive(pid) is False
    assert daemon.stop_daemon({"pid": pid}) is False
    assert kill_calls == []
    assert send_calls == []


def test_stale_untrusted_state_only_unlinks_derived_daemon_paths(tmp_path, monkeypatch):
    control_dir = daemon.daemon_dir()
    state_file = control_dir / "malicious.json"
    derived_socket = daemon.control_socket_path(state_file.stem)
    outside_victim = tmp_path / "must-not-be-unlinked.sock"
    derived_socket.write_text("stale socket placeholder", encoding="utf-8")
    outside_victim.write_text("keep me", encoding="utf-8")
    state_file.write_text(
        json.dumps(
            {
                "schema_version": daemon.STATE_SCHEMA_VERSION,
                "pid": -1,
                "port": "malicious",
                "sock": str(outside_victim),
                "session": str(tmp_path / "outside-session"),
                "instance": "a" * 32,
            }
        ),
        encoding="utf-8",
    )

    kill_calls = []
    unlink_calls: list[Path] = []
    original_unlink = Path.unlink

    def fake_kill(*args):
        kill_calls.append(args)
        raise ProcessLookupError

    def tracked_unlink(path: Path, *args, **kwargs):
        unlink_calls.append(path.resolve(strict=False))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(daemon.os, "kill", fake_kill)
    monkeypatch.setattr(Path, "unlink", tracked_unlink)

    assert daemon.list_daemons() == []
    assert kill_calls == []
    assert outside_victim.read_text(encoding="utf-8") == "keep me"
    assert not state_file.exists()
    assert not derived_socket.exists()
    assert unlink_calls
    allowed_parents = {control_dir.resolve(), daemon.socket_dir().resolve()}
    assert all(path.parent in allowed_parents for path in unlink_calls)


def test_stop_does_not_signal_an_unverified_process(monkeypatch):
    kill_calls = []

    monkeypatch.setattr(
        daemon,
        "send_cmd",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(daemon.DaemonError("offline")),
    )
    monkeypatch.setattr(daemon.os, "kill", lambda *args: kill_calls.append(args))

    assert daemon.stop_daemon({"pid": os.getpid()}) is False
    assert kill_calls == []


def test_live_reused_pid_and_orphaned_socket_do_not_validate() -> None:
    port = "/dev/cu.usbmodem-stale"
    slug = daemon.port_slug(port)
    session = daemon.sessions_dir() / "stale-session"
    daemon.ensure_private_dir(session)
    sock_path = daemon.control_socket_path(slug)
    sock_path.unlink(missing_ok=True)
    stale_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale_socket.bind(str(sock_path))
    stale_socket.close()
    state_file = daemon.state_path(slug)
    daemon.atomic_write_text(
        state_file,
        json.dumps(
            {
                "schema_version": daemon.STATE_SCHEMA_VERSION,
                "pid": os.getpid(),
                "port": port,
                "baud": 115200,
                "session": str(session),
                "sock": str(sock_path),
                "instance": "c" * 32,
            }
        ),
    )

    assert daemon.list_daemons() == []
    assert not state_file.exists()
    assert not sock_path.exists()


def test_send_cmd_rejects_a_socket_outside_the_daemon_directory(tmp_path, monkeypatch):
    socket_calls = []

    def unexpected_socket(*args, **kwargs):
        socket_calls.append((args, kwargs))
        pytest.fail("send_cmd opened a socket for untrusted state")

    monkeypatch.setattr(daemon.socket, "socket", unexpected_socket)
    state = {
        "schema_version": daemon.STATE_SCHEMA_VERSION,
        "pid": os.getpid(),
        "port": "/dev/cu.usbmodem-test",
        "sock": str(tmp_path / "attacker-controlled.sock"),
        "instance": "b" * 32,
    }

    with pytest.raises(daemon.DaemonError, match="escapes the control directory"):
        daemon.send_cmd(state, {"cmd": "status"})
    assert socket_calls == []


def test_control_artifacts_are_owner_only(running_daemon):
    state, state_file, _thread = running_daemon
    sock_path = Path(state["sock"])

    assert daemon.list_daemons()[0]["instance"] == state["instance"]
    assert stat.S_IMODE(daemon.daemon_dir().stat().st_mode) == 0o700
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert stat.S_ISSOCK(sock_path.stat().st_mode)
    assert stat.S_IMODE(sock_path.stat().st_mode) == 0o600


def test_control_socket_path_stays_below_unix_limit_for_long_storage_root(tmp_path, monkeypatch):
    long_home = tmp_path / ("a" * 80) / ("b" * 80)
    monkeypatch.setenv("HWLOG_DIR", str(long_home))

    sock_path = daemon.control_socket_path("port-with-a-long-selector-" + "c" * 100)

    assert len(os.fsencode(sock_path)) < 100
    assert stat.S_IMODE(sock_path.parent.stat().st_mode) == 0o700


def test_socket_directory_rejects_an_attacker_owned_symlink(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    monkeypatch.setenv("HWLOG_DIR", str(storage))
    root_hash = daemon.hashlib.sha256(str(storage.resolve()).encode()).hexdigest()[:12]
    socket_root = Path("/tmp") / f"hwlog-{os.getuid()}-{root_hash}"
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o755)
    socket_root.unlink(missing_ok=True)
    socket_root.symlink_to(victim, target_is_directory=True)

    try:
        with pytest.raises(daemon.DaemonError, match="unsafe control socket directory"):
            daemon.socket_dir()
        assert stat.S_IMODE(victim.stat().st_mode) == 0o755
    finally:
        socket_root.unlink(missing_ok=True)


def test_stale_cleanup_does_not_remove_a_replaced_state_or_socket(tmp_path):
    port = "/dev/cu.usbmodem-race"
    slug = daemon.port_slug(port)
    path = daemon.state_path(slug)
    path.write_text("old", encoding="utf-8")
    old_info = path.stat()
    expected = (old_info.st_dev, old_info.st_ino)
    replacement = path.with_suffix(".replacement")
    replacement.write_text("new", encoding="utf-8")
    os.replace(replacement, path)
    sock_path = daemon.control_socket_path(slug)
    sock_path.write_text("new socket placeholder", encoding="utf-8")

    daemon._cleanup_state_file(path, expected)

    assert path.read_text(encoding="utf-8") == "new"
    assert sock_path.read_text(encoding="utf-8") == "new socket placeholder"


def test_bad_control_requests_do_not_stop_the_server(running_daemon):
    state, state_file, thread = running_daemon
    token = state["instance"]
    encoded_requests = [
        b"{not-json}\n",
        b"\xff\n",
        b"[]\n",
        b'"stop"\n',
        (json.dumps({"instance": token, "cmd": ["stop"]}) + "\n").encode(),
        (json.dumps({"instance": token, "cmd": None}) + "\n").encode(),
        (
            json.dumps({"instance": token, "cmd": "send", "data": ["stop"], "newline": True}) + "\n"
        ).encode(),
        (
            json.dumps({"instance": token, "cmd": "send", "data": "stop", "newline": "yes"}) + "\n"
        ).encode(),
        (json.dumps({"instance": "wrong-token", "cmd": "stop"}) + "\n").encode(),
        b"x" * (daemon.MAX_CONTROL_BYTES + 1),
    ]

    for request in encoded_requests:
        response = _raw_request(state["sock"], request)
        assert response["ok"] is False

        status_response = daemon.send_cmd(state, {"cmd": "status"}, timeout=2)
        assert status_response["ok"] is True
        assert status_response["connected"] is False
        assert state_file.exists()
        assert thread.is_alive()


def test_pause_lease_rejects_competing_owner(running_daemon):
    state, _state_file, _thread = running_daemon
    first = "a" * 32
    second = "b" * 32

    owner_pid = os.getpid()
    assert (
        daemon.send_cmd(state, {"cmd": "pause", "lease": first, "owner_pid": owner_pid})["ok"]
        is True
    )
    rejected = daemon.send_cmd(state, {"cmd": "pause", "lease": second, "owner_pid": owner_pid})
    assert rejected["ok"] is False
    assert "owns the pause lease" in rejected["error"]
    assert daemon.send_cmd(state, {"cmd": "resume", "lease": second})["ok"] is False
    assert daemon.send_cmd(state, {"cmd": "resume", "lease": first})["ok"] is True


def test_abandoned_pause_lease_is_recovered(running_daemon):
    state, _state_file, _thread = running_daemon
    owner = subprocess.Popen(["sleep", "5"])
    lease = "d" * 32
    try:
        assert (
            daemon.send_cmd(state, {"cmd": "pause", "lease": lease, "owner_pid": owner.pid})["ok"]
            is True
        )
        owner.terminate()
        owner.wait(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = daemon.send_cmd(state, {"cmd": "status"})
            if not status["paused"]:
                break
            time.sleep(0.05)
        assert status["paused"] is False
    finally:
        with contextlib.suppress(ProcessLookupError):
            owner.kill()


def test_slow_control_client_does_not_block_status(running_daemon):
    state, _state_file, _thread = running_daemon
    slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    slow.connect(state["sock"])
    slow.sendall(b"{")
    try:
        started = time.monotonic()
        status = daemon.send_cmd(state, {"cmd": "status"}, timeout=1)
        assert status["ok"] is True
        assert time.monotonic() - started < 0.5
    finally:
        slow.close()


def test_find_daemon_requires_an_unambiguous_selector(monkeypatch):
    first = {"port": "/dev/cu.usbmodem101"}
    second = {"port": "/dev/cu.usbmodem102"}
    monkeypatch.setattr(daemon, "list_daemons", lambda: [first, second])

    with pytest.raises(daemon.AmbiguousDaemonError, match="specify --port"):
        daemon.find_daemon()
    with pytest.raises(daemon.AmbiguousDaemonError, match="matches multiple daemons"):
        daemon.find_daemon("usbmodem")

    assert daemon.find_daemon(first["port"]) is first
    assert daemon.find_daemon("not-present") is None


def test_direct_daemon_entry_refuses_an_overlapping_auto_owner(running_daemon):
    _state, _state_file, _thread = running_daemon

    with pytest.raises(daemon.DaemonError, match="overlapping capture daemon"):
        daemon.run_daemon(None, 115200)


def test_live_legacy_daemon_is_preserved_and_new_control_refuses_it(monkeypatch):
    short_home = tempfile.TemporaryDirectory(prefix="hwl-", dir="/tmp")
    monkeypatch.setenv("HWLOG_DIR", short_home.name)
    port = "/dev/cu.usbmodem-legacy"
    slug = daemon._legacy_port_slug(port)
    session = daemon.sessions_dir() / "legacy-session"
    daemon.ensure_private_dir(session)
    sock_path = daemon.daemon_dir() / f"{slug}.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    state_file = daemon.daemon_dir() / f"{slug}.json"
    daemon.atomic_write_text(
        state_file,
        json.dumps(
            {
                "pid": os.getpid(),
                "port": port,
                "baud": 115200,
                "session": str(session),
                "sock": str(sock_path),
                "started_at": "2026-08-06T00:00:00.000Z",
            }
        ),
    )

    try:
        states = daemon.list_daemons()
        assert len(states) == 1
        state = states[0]
        assert state["legacy"] is True
        assert state_file.exists()
        with pytest.raises(daemon.DaemonError, match="legacy daemon may still own"):
            daemon.start_background(port, 115200)
        with pytest.raises(daemon.DaemonError, match="unauthenticated control protocol"):
            daemon.send_cmd(state, {"cmd": "status"})
        with pytest.raises(daemon.DaemonError, match="legacy daemon may still own"):
            daemon.run_daemon(port, 115200)
        assert daemon._legacy_conflicts(state, "/dev/cu.usbmodem-other") is True
        assert daemon._legacy_conflicts(state, "/dev/tty.usbmodem-legacy") is True
        assert daemon._legacy_conflicts(state, "/dev/serial/by-id/legacy") is True
        assert daemon._legacy_conflicts(state, "/dev/cu.usbmodem") is True
        assert daemon._legacy_conflicts(state, "/dev/cu.usbmodem-legacy0") is True
        assert daemon._legacy_conflicts(state, "usbmodem") is True

        socket_calls = []
        monkeypatch.setattr(
            daemon.socket,
            "socket",
            lambda *_args, **_kwargs: socket_calls.append(True),
        )
        assert daemon.stop_daemon(state) is False
        assert socket_calls == []
    finally:
        server.close()
        state_file.unlink(missing_ok=True)
        sock_path.unlink(missing_ok=True)
        short_home.cleanup()


def test_current_selector_conflicts_fail_closed():
    state = {"schema_version": daemon.STATE_SCHEMA_VERSION, "port": "ttyUSB"}

    assert daemon._current_conflicts(state, "USB0") is True
    assert daemon._current_conflicts(state, "/dev/ttyUSB0") is True
    assert daemon._current_conflicts(state, None) is True
    assert (
        daemon._current_conflicts(
            {"schema_version": daemon.STATE_SCHEMA_VERSION, "port": "/dev/ttyUSB0"},
            "/dev/ttyUSB1",
        )
        is False
    )


def test_partial_potential_legacy_state_is_preserved_and_blocks_start(monkeypatch):
    state_file = daemon.daemon_dir() / "auto.json"
    state_file.write_text("{", encoding="utf-8")
    monkeypatch.setattr(
        daemon.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("startup must fail before spawning"),
    )

    assert daemon.list_daemons() == []
    assert state_file.exists()
    with pytest.raises(daemon.DaemonError, match="unrecognized daemon state"):
        daemon.start_background(None, 115200)
    assert state_file.exists()


def test_legacy_state_that_finishes_during_retry_still_blocks_start(monkeypatch):
    short_home = tempfile.TemporaryDirectory(prefix="hwl-", dir="/tmp")
    monkeypatch.setenv("HWLOG_DIR", short_home.name)
    port = "/dev/cu.usbmodem-racing-legacy"
    slug = daemon._legacy_port_slug(port)
    session = daemon.sessions_dir() / "legacy-session"
    daemon.ensure_private_dir(session)
    sock_path = daemon.daemon_dir() / f"{slug}.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    state_file = daemon.daemon_dir() / f"{slug}.json"
    state_file.write_text("{", encoding="utf-8")
    completed = False

    def finish_state(_delay: float) -> None:
        nonlocal completed
        if completed:
            return
        completed = True
        daemon.atomic_write_text(
            state_file,
            json.dumps(
                {
                    "pid": os.getpid(),
                    "port": port,
                    "baud": 115200,
                    "session": str(session),
                    "sock": str(sock_path),
                }
            ),
        )

    monkeypatch.setattr(daemon.time, "sleep", finish_state)
    try:
        with pytest.raises(daemon.DaemonError, match="legacy daemon may still own"):
            daemon._assert_legacy_start_safe(port)
        assert completed is True
        assert daemon.list_daemons()[0]["legacy"] is True
    finally:
        server.close()
        state_file.unlink(missing_ok=True)
        sock_path.unlink(missing_ok=True)
        short_home.cleanup()


def test_unknown_daemon_schema_is_preserved_and_blocks_start(monkeypatch):
    short_home = tempfile.TemporaryDirectory(prefix="hwl-", dir="/tmp")
    monkeypatch.setenv("HWLOG_DIR", short_home.name)
    port = "/dev/cu.usbmodem-future"
    slug = daemon.port_slug(port)
    session = daemon.sessions_dir() / "future-session"
    daemon.ensure_private_dir(session)
    sock_path = daemon.control_socket_path(slug)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    state_file = daemon.state_path(slug)
    daemon.atomic_write_text(
        state_file,
        json.dumps(
            {
                "schema_version": daemon.STATE_SCHEMA_VERSION + 1,
                "pid": os.getpid(),
                "port": port,
                "baud": 115200,
                "session": str(session),
                "sock": str(sock_path),
                "instance": "f" * 32,
            }
        ),
    )

    try:
        assert daemon.list_daemons() == []
        assert state_file.exists()
        with pytest.raises(daemon.DaemonError, match="unrecognized daemon state"):
            daemon.start_background(port, 115200)
        assert state_file.exists()
    finally:
        server.close()
        state_file.unlink(missing_ok=True)
        sock_path.unlink(missing_ok=True)
        short_home.cleanup()


def test_legacy_state_under_symlinked_storage_root_is_recognized(monkeypatch):
    real_home = tempfile.TemporaryDirectory(prefix="hwl-real-", dir="/tmp")
    link_home = Path(f"{real_home.name}-link")
    link_home.symlink_to(real_home.name, target_is_directory=True)
    monkeypatch.setenv("HWLOG_DIR", str(link_home))
    port = "/dev/cu.usbmodem-symlink-root"
    slug = daemon._legacy_port_slug(port)
    session = daemon.sessions_dir() / "legacy-session"
    daemon.ensure_private_dir(session)
    sock_path = daemon.daemon_dir() / f"{slug}.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    state_file = daemon.daemon_dir() / f"{slug}.json"
    lexical_sock = link_home / "daemon" / sock_path.name
    lexical_session = link_home / "sessions" / session.name
    daemon.atomic_write_text(
        state_file,
        json.dumps(
            {
                "pid": os.getpid(),
                "port": port,
                "baud": 115200,
                "session": str(lexical_session),
                "sock": str(lexical_sock),
            }
        ),
    )

    try:
        state = daemon.list_daemons()[0]
        assert state["legacy"] is True
        assert state_file.exists()
    finally:
        server.close()
        state_file.unlink(missing_ok=True)
        sock_path.unlink(missing_ok=True)
        link_home.unlink(missing_ok=True)
        real_home.cleanup()


def test_legacy_socket_symlink_is_never_followed(monkeypatch):
    short_home = tempfile.TemporaryDirectory(prefix="hwl-", dir="/tmp")
    monkeypatch.setenv("HWLOG_DIR", short_home.name)
    port = "/dev/cu.usbmodem-link"
    slug = daemon._legacy_port_slug(port)
    session = daemon.sessions_dir() / "legacy-session"
    daemon.ensure_private_dir(session)
    outside_socket = Path(short_home.name) / "outside.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(outside_socket))
    server.listen(1)
    legacy_socket = daemon.daemon_dir() / f"{slug}.sock"
    legacy_socket.symlink_to(outside_socket)
    state_file = daemon.daemon_dir() / f"{slug}.json"
    daemon.atomic_write_text(
        state_file,
        json.dumps(
            {
                "pid": os.getpid(),
                "port": port,
                "baud": 115200,
                "session": str(session),
                "sock": str(legacy_socket),
            }
        ),
    )

    try:
        assert daemon.list_daemons() == []
        assert state_file.exists()
        with pytest.raises(daemon.DaemonError, match="unrecognized daemon state"):
            daemon.start_background(port, 115200)
    finally:
        server.close()
        state_file.unlink(missing_ok=True)
        legacy_socket.unlink(missing_ok=True)
        outside_socket.unlink(missing_ok=True)
        short_home.cleanup()
