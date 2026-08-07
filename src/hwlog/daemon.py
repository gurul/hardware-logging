"""Background capture daemon with a Unix-socket control channel.

The daemon is the single owner of the serial port. Consumers read session
files; the socket exists only for control operations that genuinely need the
port: send-to-device, pause/resume around flashing, status, stop.

Protocol: newline-delimited JSON over a Unix socket. One request per
connection: ``{"cmd": "status"}`` → ``{"ok": true, ...}``.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

from .capture import CaptureLoop
from .records import LogRecord, SessionMeta, now_iso
from .session import SessionWriter, base_dir, port_slug

SOCK_TIMEOUT_S = 5.0


def daemon_dir() -> Path:
    d = base_dir() / "daemon"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(slug: str) -> Path:
    return daemon_dir() / f"{slug}.json"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def list_daemons() -> list[dict]:
    """Live daemons (stale state files from dead PIDs are cleaned up)."""
    out = []
    for path in daemon_dir().glob("*.json"):
        try:
            state = json.loads(path.read_text())
        except json.JSONDecodeError:
            path.unlink(missing_ok=True)
            continue
        if _pid_alive(state.get("pid", -1)):
            out.append(state)
        else:
            path.unlink(missing_ok=True)
            Path(state.get("sock", "/nonexistent")).unlink(missing_ok=True)
    return out


def send_cmd(state: dict, cmd: dict, timeout: float = SOCK_TIMEOUT_S) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(state["sock"])
        s.sendall((json.dumps(cmd) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode() or "{}")


def find_daemon(port_spec: str | None = None) -> dict | None:
    daemons = list_daemons()
    if not daemons:
        return None
    if port_spec:
        slug_frag = port_slug(port_spec)
        for d in daemons:
            if port_spec in d["port"] or slug_frag in port_slug(d["port"]):
                return d
        return None
    return daemons[0]


# -- server side -------------------------------------------------------------


def run_daemon(port_spec: str | None, baud: int, board_meta: dict | None = None) -> None:
    """Run capture + control server in this process (blocks until stopped)."""
    meta = SessionMeta(
        session_id="",
        port=port_spec or "auto",
        baud=baud,
        started_at=now_iso(),
        **(board_meta or {}),
    )
    writer = SessionWriter(meta)
    loop = CaptureLoop(writer, port_spec=port_spec, baud=baud)

    slug = port_slug(port_spec or "auto")
    sock_path = daemon_dir() / f"{slug}.sock"
    sock_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(4)
    server.settimeout(0.5)

    state = {
        "pid": os.getpid(),
        "port": port_spec or "auto",
        "baud": baud,
        "session": str(writer.dir),
        "sock": str(sock_path),
        "started_at": meta.started_at,
    }
    state_path(slug).write_text(json.dumps(state, indent=2))

    def handle(conn: socket.socket) -> None:
        with conn:
            conn.settimeout(SOCK_TIMEOUT_S)
            try:
                req = json.loads(conn.makefile().readline() or "{}")
            except (json.JSONDecodeError, OSError):
                return
            resp = dispatch(req)
            with contextlib.suppress(OSError):
                conn.sendall((json.dumps(resp) + "\n").encode())

    def dispatch(req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "status":
            return {
                "ok": True,
                "connected": loop.connected,
                "paused": loop.pause_event.is_set(),
                "session": str(writer.dir),
                "boots": writer.meta.boots,
                "crashes": writer.meta.crashes,
                "records": writer.record_count,
            }
        if cmd == "send":
            data = req.get("data", "")
            if req.get("newline", True):
                data += "\n"
            ok = loop.send(data.encode())
            return {"ok": ok, "error": None if ok else "port not connected"}
        if cmd == "pause":
            loop.pause()
            return {"ok": True}
        if cmd == "resume":
            loop.resume()
            return {"ok": True}
        if cmd == "add_elf":
            writer.add_elf(req.get("path", ""))
            return {"ok": True}
        if cmd == "stop":
            loop.stop()
            return {"ok": True}
        return {"ok": False, "error": f"unknown cmd: {cmd}"}

    def serve() -> None:
        while not loop.stop_event.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            handle(conn)

    control = threading.Thread(target=serve, name="hwlog-control", daemon=True)
    control.start()

    def on_term(_sig, _frm):
        loop.stop()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    try:
        loop.run()  # blocks; closes writer on exit
    finally:
        server.close()
        sock_path.unlink(missing_ok=True)
        state_path(slug).unlink(missing_ok=True)


def start_background(port_spec: str | None, baud: int) -> dict:
    """Spawn a detached daemon process; returns its state dict."""
    slug = port_slug(port_spec or "auto")
    existing = find_daemon(port_spec)
    if existing:
        return existing
    out_path = daemon_dir() / f"{slug}.out"
    args = [sys.executable, "-m", "hwlog.cli", "_daemon", "--baud", str(baud)]
    if port_spec:
        args += ["--port", port_spec]
    with out_path.open("ab") as out:
        subprocess.Popen(
            args,
            stdout=out,
            stderr=out,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )
    # wait briefly for the state file to appear
    import time

    for _ in range(50):
        if state_path(slug).exists():
            return json.loads(state_path(slug).read_text())
        time.sleep(0.1)
    raise RuntimeError(f"daemon did not start; see {out_path}")


def stop_daemon(state: dict) -> bool:
    try:
        send_cmd(state, {"cmd": "stop"})
    except OSError:
        with contextlib.suppress(OSError):
            os.kill(state["pid"], signal.SIGTERM)
    import time

    for _ in range(50):
        if not _pid_alive(state["pid"]):
            return True
        time.sleep(0.1)
    return False


def append_note(session_dir: str, msg: str) -> None:
    """Append a host-side status record to a session log (e.g. flash notes)."""
    path = Path(session_dir) / "log.jsonl"
    rec = LogRecord(ts=now_iso(), seq=0, boot=0, event="status", msg=msg)
    with path.open("a", encoding="utf-8") as f:
        f.write(rec.to_json() + "\n")
