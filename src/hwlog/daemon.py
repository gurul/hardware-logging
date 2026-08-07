"""Background capture daemon with a Unix-socket control channel.

The daemon is the single owner of the serial port. Consumers read session
files; the socket exists only for control operations that genuinely need the
port: send-to-device, pause/resume around flashing, status, stop.

Protocol: newline-delimited JSON over a Unix socket. One request per
connection: ``{"cmd": "status"}`` → ``{"ok": true, ...}``.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .capture import MAX_SEND_BYTES, CaptureLoop
from .records import SessionMeta, now_iso
from .session import (
    SessionWriter,
    atomic_write_text,
    base_dir,
    ensure_private_dir,
    port_match_key,
    port_slug,
    sessions_dir,
)

SOCK_TIMEOUT_S = 5.0
PAUSE_TIMEOUT_S = 5.0
# Hard ceiling on how long a flash pause may keep capture off. Backs up the
# owner-PID liveness check: if the flasher's PID is reused by a long-lived
# process, the lease would otherwise never be recovered.
PAUSE_LEASE_MAX_S = 600.0
STOP_TIMEOUT_S = 15.0
MAX_CONTROL_BYTES = 16 * 1024
MAX_NOTE_CHARS = 4096
MAX_CONTROL_WORKERS = 8
STATE_SCHEMA_VERSION = 2


class DaemonError(RuntimeError):
    """The daemon control channel is unavailable or returned invalid data."""


class AmbiguousDaemonError(DaemonError):
    """A command needs an explicit port because multiple daemons match."""


def daemon_dir() -> Path:
    ensure_private_dir(base_dir())
    return ensure_private_dir(base_dir() / "daemon")


def state_path(slug: str) -> Path:
    return daemon_dir() / f"{slug}.json"


def socket_dir() -> Path:
    """Return a private, short path suitable for AF_UNIX's small path limit."""
    root_hash = hashlib.sha256(str(base_dir()).encode()).hexdigest()[:12]
    # AF_UNIX paths are short on macOS, so use the system short-path root. The
    # derived directory is opened with O_NOFOLLOW and verified owner-only below.
    system_tmp = Path("/tmp")  # nosec B108
    temp_root = system_tmp if system_tmp.is_dir() else Path(tempfile.gettempdir())
    path = temp_root / f"hwlog-{os.getuid()}-{root_hash}"
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DaemonError(f"refusing unsafe control socket directory: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise DaemonError(f"control socket directory is not owned by uid {os.getuid()}")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return path


def control_socket_path(slug: str) -> Path:
    slug_hash = hashlib.sha256(slug.encode()).hexdigest()[:16]
    return socket_dir() / f"{slug_hash}.sock"


def _pid_alive(pid: object) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _open_daemon_lock(slug: str, *, create: bool) -> object:
    path = daemon_dir() / f"{slug}.lock"
    flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise DaemonError(f"refusing unsafe daemon lock: {path}")
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "a+")


def _cleanup_state_file(path: Path, expected_inode: tuple[int, int] | None = None) -> None:
    """Remove stale derived paths while holding the same lock as daemon startup."""
    try:
        lock = _open_daemon_lock(path.stem, create=True)
    except (OSError, DaemonError):
        return
    with lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            if expected_inode is not None:
                current = path.lstat()
                if (current.st_dev, current.st_ino) != expected_inode:
                    return
            path.unlink(missing_ok=True)
            control_socket_path(path.stem).unlink(missing_ok=True)
        except OSError:
            return
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _daemon_lock_is_held(slug: str) -> bool:
    """A live daemon owns this advisory lock for its entire lifetime."""
    try:
        lock = _open_daemon_lock(slug, create=False)
    except (OSError, DaemonError):
        return False
    with lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return False


def _legacy_port_slug(port: str) -> str:
    """Reproduce the released state/socket name for upgrade detection only."""
    return re.sub(r"[^A-Za-z0-9]+", "-", port).strip("-").lower()


def _canonical_parent_path(value: str) -> Path:
    """Resolve parent-directory aliases while never following the final node."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.parent.resolve(strict=True) / path.name


def _read_state_bytes(path: Path, expected_inode: tuple[int, int]) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or (info.st_dev, info.st_ino) != expected_inode
            or info.st_size > MAX_CONTROL_BYTES
        ):
            raise OSError("unsafe daemon state file")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            data = stream.read(MAX_CONTROL_BYTES + 1)
        if len(data) > MAX_CONTROL_BYTES:
            raise OSError("daemon state file is too large")
        return data
    finally:
        if fd >= 0:
            os.close(fd)


def _legacy_state_fields_ok(
    path: Path,
    raw: dict,
    *,
    pid: object,
    port: object,
    sock: object,
    session: object,
) -> bool:
    """Structurally complete legacy state (says nothing about liveness)."""
    return not (
        raw.get("schema_version") is not None
        or raw.get("instance") is not None
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or not isinstance(port, str)
        or not port
        or not isinstance(sock, str)
        or not isinstance(session, str)
        or len(port) > 4096
        or len(sock) > 4096
        or len(session) > 4096
        or path.stem != _legacy_port_slug(port)
    )


def _valid_legacy_state(
    path: Path,
    raw: dict,
    *,
    pid: object,
    port: object,
    sock: object,
    session: object,
) -> dict | None:
    """Preserve a live pre-authentication daemon without trusting its controls."""
    if not _legacy_state_fields_ok(path, raw, pid=pid, port=port, sock=sock, session=session):
        return None
    if not _pid_alive(pid):
        return None
    try:
        expected_sock = daemon_dir() / f"{path.stem}.sock"
        provided_sock = _canonical_parent_path(sock)
        session_path = _canonical_parent_path(session)
        session_root = sessions_dir()
        sock_info = expected_sock.lstat()
        session_info = session_path.lstat()
    except (OSError, RuntimeError, ValueError):
        return None
    if (
        provided_sock != expected_sock
        or session_path.parent != session_root
        or expected_sock.is_symlink()
        or not stat.S_ISSOCK(sock_info.st_mode)
        or sock_info.st_uid != os.getuid()
        or session_path.is_symlink()
        or not stat.S_ISDIR(session_info.st_mode)
        or session_info.st_uid != os.getuid()
    ):
        return None
    return {**raw, "legacy": True}


def _read_valid_state(path: Path) -> dict | None:
    try:
        path_info = path.lstat()
        expected_inode = (path_info.st_dev, path_info.st_ino)
        if (
            path.is_symlink()
            or not stat.S_ISREG(path_info.st_mode)
            or path_info.st_uid != os.getuid()
            or path_info.st_size > MAX_CONTROL_BYTES
        ):
            _cleanup_state_file(path, expected_inode)
            return None
    except OSError:
        return None
    try:
        raw = json.loads(_read_state_bytes(path, expected_inode).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        # This may be a released daemon racing its old non-atomic state write.
        # Its process holds no new-style lock, so deletion could orphan it.
        return None
    if not isinstance(raw, dict):
        return None
    pid = raw.get("pid")
    port = raw.get("port")
    sock = raw.get("sock")
    session = raw.get("session")
    instance = raw.get("instance")
    if instance is None:
        legacy = _valid_legacy_state(
            path,
            raw,
            pid=pid,
            port=port,
            sock=sock,
            session=session,
        )
        if legacy is not None:
            return legacy
        if (
            _legacy_state_fields_ok(path, raw, pid=pid, port=port, sock=sock, session=session)
            and not _pid_alive(pid)
            and not _daemon_lock_is_held(path.stem)
        ):
            # Structurally complete legacy state whose recorded owner is
            # provably dead: cleaning it cannot orphan a live daemon, and
            # preserving it would block every future start.
            _cleanup_state_file(path, expected_inode)
            return None
        # Other no-token state may belong to a live released daemon (for
        # example a torn mid-write file). Preserve it and make startup fail
        # closed until an operator verifies it.
        return None
    if raw.get("schema_version") != STATE_SCHEMA_VERSION:
        # A future/unknown authenticated protocol must not receive current
        # commands or be deleted using current cleanup assumptions.
        return None
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or not isinstance(port, str)
        or not isinstance(sock, str)
        or not isinstance(session, str)
        or not isinstance(instance, str)
        or len(instance) < 16
    ):
        _cleanup_state_file(path, expected_inode)
        return None
    try:
        if len(port) > 4096 or len(sock) > 4096 or len(session) > 4096:
            raise ValueError
        expected_sock = _canonical_parent_path(str(control_socket_path(path.stem)))
        provided_sock = _canonical_parent_path(sock)
        session_path = _canonical_parent_path(session)
        session_root = sessions_dir()
        sock_info = expected_sock.lstat()
        session_info = session_path.lstat()
    except (OSError, RuntimeError, ValueError):
        _cleanup_state_file(path, expected_inode)
        return None
    if (
        path.stem != port_slug(port)
        or provided_sock != expected_sock
        or session_path.parent != session_root
        or expected_sock.is_symlink()
        or not stat.S_ISSOCK(sock_info.st_mode)
        or sock_info.st_uid != os.getuid()
        or session_path.is_symlink()
        or not stat.S_ISDIR(session_info.st_mode)
        or session_info.st_uid != os.getuid()
        or not _pid_alive(pid)
        or not _daemon_lock_is_held(path.stem)
    ):
        _cleanup_state_file(path, expected_inode)
        return None
    return raw


def list_daemons() -> list[dict]:
    """Live daemons (stale state files from dead PIDs are cleaned up)."""
    out = []
    for path in sorted(daemon_dir().glob("*.json")):
        state = _read_valid_state(path)
        if state is not None:
            out.append(state)
    return out


def _assert_no_unresolved_state_files() -> None:
    """Fail closed on state that could be a partially written legacy daemon."""
    for attempt in range(3):
        unresolved = [
            path
            for path in daemon_dir().glob("*.json")
            if _read_valid_state(path) is None and path.exists()
        ]
        if not unresolved:
            return
        if attempt < 2:
            time.sleep(0.05)
    raise DaemonError(
        "unrecognized daemon state is present; retry, or verify no legacy daemon is running "
        "before removing stale files from the hwlog daemon directory"
    )


def _state_snapshot() -> tuple[tuple[str, int, int, int, int], ...]:
    snapshot = []
    for path in daemon_dir().glob("*.json"):
        try:
            info = path.lstat()
        except OSError:
            continue
        snapshot.append((path.name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
    return tuple(sorted(snapshot))


def _legacy_conflicts(state: dict) -> bool:
    if state.get("legacy") is not True:
        return False
    # Legacy state has no physical USB identity or ownership lock. Distinct
    # device paths can still alias one board (for example macOS cu/tty siblings
    # or Linux by-id links), so non-overlap cannot be proven safely. Require all
    # legacy owners to stop before starting any authenticated daemon.
    return True


def _current_conflicts(state: dict, port_spec: str | None) -> bool:
    if state.get("legacy") is True:
        return False
    existing_port = state.get("port")
    if not isinstance(existing_port, str):
        return True
    # Selectors are deliberately retained so capture can follow a renumbered
    # USB device. Consequently, two substring selectors that look unrelated
    # today may both resolve to the same path later (``ttyUSB`` and ``USB0``).
    # Only distinct explicit device paths can be proven non-overlapping here;
    # the physical-device lock remains the final guard against path aliases.
    exact_request = isinstance(port_spec, str) and port_spec.startswith("/dev/")
    exact_existing = existing_port.startswith("/dev/")
    overlaps = isinstance(port_spec, str) and (
        port_spec in existing_port or existing_port in port_spec
    )
    return not (exact_request and exact_existing and not overlaps)


def _assert_legacy_start_safe(
    port_spec: str | None,
    *,
    check_current: bool = False,
) -> None:
    states: list[dict] = []
    for attempt in range(3):
        before = _state_snapshot()
        _assert_no_unresolved_state_files()
        states = list_daemons()
        after = _state_snapshot()
        if before == after:
            break
        if attempt < 2:
            time.sleep(0.05)
    else:
        raise DaemonError("daemon state is changing; retry after the existing daemon settles")
    if any(_legacy_conflicts(state) for state in states):
        raise DaemonError(
            "a legacy daemon may still own this serial port; stop it with the previously installed "
            "hwlog version, or verify and terminate its recorded PID, then retry"
        )
    if check_current and any(_current_conflicts(state, port_spec) for state in states):
        raise DaemonError("a potentially overlapping capture daemon is already running")


def _recv_line(
    conn: socket.socket,
    maximum: int = MAX_CONTROL_BYTES,
    *,
    deadline: float | None = None,
) -> bytes:
    data = bytearray()
    while len(data) <= maximum:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DaemonError("control request deadline exceeded")
            conn.settimeout(remaining)
        chunk = conn.recv(min(4096, maximum + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        newline = data.find(b"\n")
        if newline >= 0:
            return bytes(data[:newline])
    if len(data) > maximum:
        raise DaemonError(f"control message exceeds {maximum} bytes")
    return bytes(data)


def send_cmd(state: dict, cmd: dict, timeout: float = SOCK_TIMEOUT_S) -> dict:
    if state.get("legacy") is True:
        raise DaemonError(
            "legacy daemon uses an unauthenticated control protocol; stop it with the "
            "previously installed hwlog version, or verify and terminate its recorded PID"
        )
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise DaemonError("unsupported daemon state schema")
    port = state.get("port")
    sock = state.get("sock")
    instance = state.get("instance")
    if not isinstance(port, str) or not isinstance(sock, str) or not isinstance(instance, str):
        raise DaemonError("invalid daemon state")
    expected = control_socket_path(port_slug(port)).resolve(strict=False)
    if Path(sock).resolve(strict=False) != expected:
        raise DaemonError("daemon socket escapes the control directory")
    payload = (json.dumps({**cmd, "instance": instance}) + "\n").encode()
    if len(payload) > MAX_CONTROL_BYTES:
        raise DaemonError(f"control message exceeds {MAX_CONTROL_BYTES} bytes")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(expected))
        s.sendall(payload)
        data = _recv_line(s, deadline=time.monotonic() + timeout)
    try:
        response = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DaemonError("daemon returned malformed JSON") from exc
    if not isinstance(response, dict):
        raise DaemonError("daemon response must be a JSON object")
    return response


def find_daemon(port_spec: str | None = None) -> dict | None:
    daemons = list_daemons()
    if not daemons:
        return None
    if port_spec:
        # port_slug appends a per-string hash, so slug containment can never
        # match; compare hashless normalized forms for fragment selectors.
        match_frag = port_match_key(port_spec)
        exact = [d for d in daemons if d["port"] == port_spec]
        matches = exact or [
            d
            for d in daemons
            if port_spec in d["port"] or (match_frag and match_frag in port_match_key(d["port"]))
        ]
        if len(matches) > 1:
            raise AmbiguousDaemonError(
                f"port selector {port_spec!r} matches multiple daemons; use an exact port"
            )
        return matches[0] if matches else None
    if len(daemons) > 1:
        raise AmbiguousDaemonError("multiple daemons are running; specify --port")
    return daemons[0]


# -- server side -------------------------------------------------------------


def run_daemon(port_spec: str | None, baud: int, board_meta: dict | None = None) -> None:
    """Run capture + control server in this process (blocks until stopped)."""
    if baud <= 0:
        raise ValueError("baud must be positive")
    _assert_legacy_start_safe(port_spec, check_current=True)
    slug = port_slug(port_spec or "auto")
    lock = _open_daemon_lock(slug, create=True)
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise DaemonError(f"a daemon for {port_spec or 'auto'} is already starting") from exc
    except BaseException:
        lock.close()
        raise
    try:
        _assert_legacy_start_safe(port_spec, check_current=True)
    except BaseException:
        with contextlib.suppress(OSError):
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
        raise

    writer: SessionWriter | None = None
    loop: CaptureLoop | None = None
    server: socket.socket | None = None
    try:
        meta = SessionMeta(
            session_id="",
            port=port_spec or "auto",
            baud=baud,
            started_at=now_iso(),
            **(board_meta or {}),
        )
        writer = SessionWriter(meta)
        loop = CaptureLoop(writer, port_spec=port_spec, baud=baud)
        sock_path = control_socket_path(slug)
        sock_path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except BaseException:
        if server is not None:
            server.close()
        if loop is not None:
            loop._decoder.shutdown(wait=False, cancel_futures=True)
            loop._release_device_ownership()
        if writer is not None:
            writer.close()
        with contextlib.suppress(OSError):
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
        raise

    assert writer is not None and loop is not None and server is not None
    control: threading.Thread | None = None
    lease_watchdog: threading.Thread | None = None
    control_workers: ThreadPoolExecutor | None = None
    state_created = False
    try:
        server.bind(str(sock_path))
        sock_path.chmod(0o600)
        server.listen(4)
        server.settimeout(0.5)

        state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "pid": os.getpid(),
            "port": port_spec or "auto",
            "baud": baud,
            "session": str(writer.dir),
            "sock": str(sock_path),
            "started_at": meta.started_at,
            "instance": uuid.uuid4().hex,
        }
        atomic_write_text(state_path(slug), json.dumps(state, indent=2))
        state_created = True
        pause_lease: str | None = None
        pause_owner_pid: int | None = None
        pause_started: float | None = None
        pause_guard = threading.Lock()

        def handle(conn: socket.socket) -> None:
            with conn:
                conn.settimeout(SOCK_TIMEOUT_S)
                try:
                    raw = _recv_line(conn, deadline=time.monotonic() + SOCK_TIMEOUT_S)
                    req = json.loads(raw.decode("utf-8"))
                    if not isinstance(req, dict):
                        raise ValueError("request must be a JSON object")
                    resp = dispatch(req)
                except (DaemonError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    resp = {"ok": False, "error": str(exc)}
                except Exception:
                    resp = {"ok": False, "error": "internal control error"}
                payload = (json.dumps(resp) + "\n").encode()
                if len(payload) > MAX_CONTROL_BYTES:
                    # Substitute a valid error object; a byte-truncated reply
                    # would be malformed JSON at the client.
                    payload = (
                        json.dumps({"ok": False, "error": "response too large"}) + "\n"
                    ).encode()
                with contextlib.suppress(OSError):
                    conn.sendall(payload)

        def dispatch(req: dict) -> dict:
            nonlocal pause_lease, pause_owner_pid, pause_started
            if req.get("instance") != state["instance"]:
                return {"ok": False, "error": "invalid daemon instance token"}
            cmd = req.get("cmd")
            if not isinstance(cmd, str):
                return {"ok": False, "error": "cmd must be a string"}
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
                newline = req.get("newline", True)
                if not isinstance(data, str) or not isinstance(newline, bool):
                    return {"ok": False, "error": "send data must be text and newline a boolean"}
                if newline:
                    data += "\n"
                encoded = data.encode("utf-8")
                if len(encoded) > MAX_SEND_BYTES:
                    return {"ok": False, "error": f"send payload exceeds {MAX_SEND_BYTES} bytes"}
                ok = loop.send(encoded)
                error = None if ok else "port not connected, write timed out, or payload too large"
                return {"ok": ok, "error": error}
            if cmd == "pause":
                lease = req.get("lease")
                owner_pid = req.get("owner_pid")
                if not isinstance(lease, str) or not 16 <= len(lease) <= 128:
                    return {"ok": False, "error": "pause requires a valid lease token"}
                if (
                    isinstance(owner_pid, bool)
                    or not isinstance(owner_pid, int)
                    or owner_pid <= 1
                    or not _pid_alive(owner_pid)
                ):
                    return {"ok": False, "error": "pause requires a live owner PID"}
                with pause_guard:
                    if pause_lease is not None and pause_lease != lease:
                        return {
                            "ok": False,
                            "error": "another flash operation owns the pause lease",
                        }
                    pause_lease = lease
                    pause_owner_pid = owner_pid
                    pause_started = time.monotonic()
                    ok = loop.pause(timeout=PAUSE_TIMEOUT_S)
                    if not ok:
                        loop.resume()
                        pause_lease = None
                        pause_owner_pid = None
                        pause_started = None
                return {"ok": ok, "error": None if ok else "timed out releasing serial port"}
            if cmd == "resume":
                lease = req.get("lease")
                awaiting_elf = req.get("awaiting_elf", False)
                if not isinstance(awaiting_elf, bool):
                    return {"ok": False, "error": "awaiting_elf must be a boolean"}
                with pause_guard:
                    if pause_lease is None or lease != pause_lease:
                        return {"ok": False, "error": "pause lease token does not match"}
                    generation = loop.resume(
                        awaiting_elf=awaiting_elf,
                        record_flash_boundary=awaiting_elf,
                    )
                    pause_lease = None
                    pause_owner_pid = None
                    pause_started = None
                return {"ok": True, "firmware_generation": generation}
            if cmd == "note":
                message = req.get("message")
                if not isinstance(message, str) or len(message) > MAX_NOTE_CHARS:
                    return {"ok": False, "error": f"note must be at most {MAX_NOTE_CHARS} chars"}
                loop.note(message)
                return {"ok": True}
            if cmd == "add_elf":
                value = req.get("path")
                generation = req.get("firmware_generation")
                if (
                    not isinstance(value, str)
                    or isinstance(generation, bool)
                    or not isinstance(generation, int)
                    or generation < 0
                ):
                    return {
                        "ok": False,
                        "error": "ELF path and non-negative firmware generation are required",
                    }
                path = Path(value)
                archive = (base_dir() / "elf-archive").resolve(strict=False)
                resolved = path.resolve(strict=False)
                if (
                    path.is_symlink()
                    or resolved.parent != archive
                    or resolved.suffix.lower() != ".elf"
                    or not resolved.is_file()
                ):
                    return {"ok": False, "error": "ELF must be a regular archived .elf file"}
                with pause_guard:
                    loop.add_elf(str(resolved), generation=generation)
                return {"ok": True}
            if cmd == "stop":
                loop.stop()
                return {"ok": True}
            return {"ok": False, "error": f"unknown cmd: {cmd}"}

        control_slots = threading.BoundedSemaphore(MAX_CONTROL_WORKERS)
        control_workers = ThreadPoolExecutor(
            max_workers=MAX_CONTROL_WORKERS,
            thread_name_prefix="hwlog-control-client",
        )

        def handle_with_slot(conn: socket.socket) -> None:
            try:
                handle(conn)
            finally:
                control_slots.release()

        def serve() -> None:
            while not loop.stop_event.is_set():
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not control_slots.acquire(blocking=False):
                    with conn:
                        with contextlib.suppress(OSError):
                            conn.sendall(b'{"ok":false,"error":"control server busy"}\n')
                    continue
                try:
                    control_workers.submit(handle_with_slot, conn)
                except RuntimeError:
                    control_slots.release()
                    conn.close()
                    break

        control = threading.Thread(target=serve, name="hwlog-control", daemon=True)
        control.start()

        def recover_abandoned_pause() -> None:
            nonlocal pause_lease, pause_owner_pid, pause_started
            while not loop.stop_event.wait(0.5):
                with pause_guard:
                    if pause_lease is None:
                        continue
                    # The owner-PID check catches a dead flasher; the lease
                    # deadline catches its PID being reused by a long-lived
                    # process, which would otherwise leave capture off forever.
                    expired = (
                        pause_started is not None
                        and time.monotonic() - pause_started > PAUSE_LEASE_MAX_S
                    )
                    if expired or not _pid_alive(pause_owner_pid):
                        pause_lease = None
                        pause_owner_pid = None
                        pause_started = None
                        # The abandoned flash may or may not have reached the
                        # device. Assume it did: a lost decode is recoverable,
                        # decoding against the wrong firmware's ELF is not.
                        loop.resume(awaiting_elf=True, record_flash_boundary=True)

        lease_watchdog = threading.Thread(
            target=recover_abandoned_pause,
            name="hwlog-pause-watchdog",
            daemon=True,
        )
        lease_watchdog.start()

        def on_term(_sig, _frm):
            loop.stop()

        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)

        loop.run()  # blocks; closes writer on exit
    finally:
        server.close()
        sock_path.unlink(missing_ok=True)
        if state_created:
            state_path(slug).unlink(missing_ok=True)
        loop.stop()
        writer.close()
        if control is not None:
            control.join(timeout=1)
        if lease_watchdog is not None:
            lease_watchdog.join(timeout=1)
        if control_workers is not None:
            control_workers.shutdown(wait=True, cancel_futures=True)
        with contextlib.suppress(OSError):
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def start_background(port_spec: str | None, baud: int) -> dict:
    """Spawn a detached daemon process; returns its state dict."""
    if baud <= 0:
        raise ValueError("baud must be positive")
    slug = port_slug(port_spec or "auto")
    _assert_legacy_start_safe(port_spec)
    running = list_daemons()
    if port_spec is not None and any(state.get("port") == "auto" for state in running):
        raise DaemonError(
            "an auto-select daemon is already running; stop it before starting an explicit port"
        )
    existing = find_daemon(port_spec)
    if existing:
        if existing.get("legacy") is True:
            raise DaemonError(
                "a legacy daemon is still running; stop it with the previously installed hwlog "
                "version, or verify and terminate its recorded PID, before restarting"
            )
        if existing.get("baud") != baud:
            raise DaemonError(
                f"daemon already runs at {existing.get('baud')} baud; stop it before changing baud"
            )
        return existing
    # ``find_daemon`` can establish idempotence for an exact or containing
    # selector, but two different substring selectors can still converge on
    # the same device after a replug. Fail before spawning; the child repeats
    # this check to close the parent/child race.
    _assert_legacy_start_safe(port_spec, check_current=True)
    # Fail fast on an ambiguous live selection while still allowing a daemon to
    # start with no board attached and wait for a later connection. Resolve only
    # after the idempotent existing-daemon path, since USB topology may change.
    from .ports import resolve_port

    resolve_port(port_spec)
    out_path = daemon_dir() / f"{slug}.out"
    args = [sys.executable, "-m", "hwlog.cli", "_daemon", "--baud", str(baud)]
    if port_spec:
        args += ["--port", port_spec]
    with out_path.open("ab") as out:
        out_path.chmod(0o600)
        proc = subprocess.Popen(
            args,
            stdout=out,
            stderr=out,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )
    for _ in range(50):
        if state_path(slug).exists() and (state := _read_valid_state(state_path(slug))):
            return state
        time.sleep(0.1)
    if proc.poll() is None:
        with contextlib.suppress(OSError):
            proc.terminate()
    raise RuntimeError(f"daemon did not start; see {out_path}")


def stop_daemon(state: dict) -> bool:
    pid = state.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    if state.get("legacy") is True:
        # The released protocol is unauthenticated and cannot prove that the
        # socket peer is the PID recorded in state. Never send a mutating command.
        return False
    try:
        response = send_cmd(state, {"cmd": "stop"})
        if not response.get("ok"):
            return False
    except (OSError, DaemonError):
        # The control channel is unavailable or wedged. Never signal a PID
        # from stale state — PID reuse makes that capable of terminating an
        # unrelated process. But a freshly re-validated state proves the slug
        # flock is held right now, and the flock holder is the process that
        # recorded its own PID, so SIGTERM against that PID cannot land on a
        # reused stranger. Without this fallback a daemon whose control
        # thread is blocked can never be stopped by hwlog at all.
        port = state.get("port")
        if not isinstance(port, str):
            return False
        current = _read_valid_state(state_path(port_slug(port)))
        if (
            current is None
            or current.get("pid") != pid
            or current.get("instance") != state.get("instance")
        ):
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False

    for _ in range(int(STOP_TIMEOUT_S * 10)):
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return False
