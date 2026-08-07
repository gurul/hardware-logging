"""Flash-safe wrapper: pause capture, run the flash tool, resume, archive the ELF.

Two field-proven rules live here:

1. **The port owner must step aside during flashing.** esptool against a port
   held by a monitor fails in ways that look exactly like a bricked board.
2. **Archive the ELF at flash time.** A panic backtrace is only symbolizable
   against the ELF that produced it; if you only keep the latest build, the
   one crash you care about becomes undecodable the moment you rebuild.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import daemon, ports
from .session import (
    base_dir,
    configured_storage_limits,
    ensure_private_dir,
    port_match_key,
    port_slug,
    read_meta,
    release_global_bytes_locked,
    reserve_global_bytes_locked,
    storage_quota_guard,
)

ELF_SEARCH_GLOBS = (
    "build/*.elf",  # ESP-IDF
    ".pio/build/*/*.elf",  # PlatformIO
    "**/*.ino.elf",  # arduino-cli export/build dirs
    "*.elf",
)
ELF_SEARCH_LIMIT = 2000  # files scanned per glob; keeps ** patterns bounded
MAX_ELF_BYTES = 512 * 1024 * 1024
MAX_ELF_SNAPSHOT_BYTES = 512 * 1024 * 1024
_FLASH_LOCKS: dict[str, threading.Lock] = {}
_FLASH_LOCKS_GUARD = threading.Lock()


def elf_archive_dir() -> Path:
    ensure_private_dir(base_dir())
    return ensure_private_dir(base_dir() / "elf-archive")


def _elf_candidates(root: Path) -> tuple[list[Path], bool]:
    """Return bounded, unique regular candidates and whether discovery was complete."""
    root = root.resolve()
    candidates: dict[Path, None] = {}
    for pattern in ELF_SEARCH_GLOBS:
        for index, path in enumerate(root.glob(pattern)):
            if index >= ELF_SEARCH_LIMIT or len(candidates) >= ELF_SEARCH_LIMIT:
                return list(candidates), False
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
                info = resolved.stat()
                if stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_ELF_BYTES:
                    candidates[resolved] = None
            except (OSError, ValueError):
                continue
    return list(candidates), True


def _hash_elf(path: Path) -> tuple[str, int] | None:
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ELF_BYTES:
            return None
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest(), info.st_size
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def _snapshot_elfs(root: Path) -> tuple[dict[Path, str], bool]:
    candidates, complete = _elf_candidates(root)
    snapshots: dict[Path, str] = {}
    total = 0
    for path in candidates:
        hashed = _hash_elf(path)
        if hashed is None:
            complete = False
            continue
        digest, size = hashed
        total += size
        if total > MAX_ELF_SNAPSHOT_BYTES:
            return snapshots, False
        snapshots[path] = digest
    return snapshots, complete


def _find_changed_elf(root: Path, before: tuple[dict[Path, str], bool]) -> tuple[Path, str] | None:
    """Select exactly one new/content-changed ELF from a complete snapshot."""
    previous, before_complete = before
    current, current_complete = _snapshot_elfs(root)
    if not before_complete or not current_complete:
        return None
    changed = [path for path, digest in current.items() if previous.get(path) != digest]
    if len(changed) != 1:
        return None
    path = changed[0]
    return path, current[path]


def find_recent_elf(root: Path, since_ts: float) -> Path | None:
    """Return one unambiguous fresh ELF, never guess among multiple targets."""
    candidates: dict[Path, float] = {}
    paths, complete = _elf_candidates(root)
    if not complete:
        return None
    for path in paths:
        try:
            mtime = path.stat().st_mtime
            if mtime >= since_ts:
                candidates[path] = mtime
        except OSError:
            continue
    if len(candidates) != 1:
        return None
    return next(iter(candidates))


def _matching_digest(first: Path, second: Path) -> str | None:
    first_hash = _hash_elf(first)
    second_hash = _hash_elf(second)
    if first_hash is None or second_hash is None or first_hash != second_hash:
        return None
    return first_hash[0]


def _select_target_daemon(states: list[dict], port_spec: str | None) -> dict | None:
    if not states:
        return None
    if port_spec is None:
        if len(states) > 1:
            raise daemon.AmbiguousDaemonError("multiple daemons are running; specify --port")
        return states[0]

    exact = [state for state in states if state.get("port") == port_spec]
    # Fragments compare against hashless normalized forms (same rule as
    # daemon.find_daemon); port_slug values are hash-suffixed and only ever
    # equal for identical inputs, which `exact` already covers.
    match_frag = port_match_key(port_spec)
    logical = exact or [
        state
        for state in states
        if port_spec in str(state.get("port", ""))
        or (match_frag and match_frag in port_match_key(str(state.get("port", ""))))
    ]
    try:
        board = ports.resolve_port(port_spec)
    except ports.AmbiguousPortError as exc:
        raise daemon.DaemonError(str(exc)) from exc
    physical = []
    if board is not None:
        for state in states:
            try:
                meta = read_meta(Path(state["session"]))
            except (OSError, UnicodeDecodeError, RecursionError, TypeError, ValueError):
                continue
            if board.serial_number and meta.usb_serial == board.serial_number:
                physical.append(state)
            elif (
                not board.serial_number
                and board.location
                and meta.usb_serial is None
                and meta.usb_location == board.location
            ):
                physical.append(state)
            elif (
                not board.serial_number
                and not board.location
                and meta.usb_serial is None
                and meta.usb_location is None
                and (meta.usb_vid, meta.usb_pid) == (board.vid, board.pid)
            ):
                physical.append(state)
    if physical:
        connected = []
        for state in physical:
            try:
                if daemon.send_cmd(state, {"cmd": "status"}, timeout=1).get("connected"):
                    connected.append(state)
            except (OSError, daemon.DaemonError):
                continue
        if len(connected) == 1:
            return connected[0]
        if len(physical) == 1:
            return physical[0]
    if len(logical) == 1:
        return logical[0]
    if len(states) == 1:
        # An auto daemon is the only possible hwlog owner; pause it even when
        # the caller names the concrete device path it selected.
        return states[0]
    raise daemon.AmbiguousDaemonError(
        f"cannot identify which daemon owns {port_spec!r}; stop duplicate selectors first"
    )


def _state_meta(state: dict):
    try:
        session = state.get("session")
        if not isinstance(session, str):
            return None
        return read_meta(Path(session))
    except (OSError, UnicodeDecodeError, RecursionError, TypeError, ValueError):
        return None


def _meta_strongly_matches_board(meta, board) -> bool:
    """Match an intrinsic serial or an explicit topology location.

    A serial mismatch is authoritative even when a USB port location happens to
    be reused by another board.
    """
    board_serial = getattr(board, "serial_number", None)
    board_location = getattr(board, "location", None)
    if meta.usb_serial is not None and board_serial is not None:
        return meta.usb_serial == board_serial
    return (
        meta.usb_location is not None
        and board_location is not None
        and meta.usb_location == board_location
    )


def _meta_matches_board(meta, board) -> bool:
    """Conservatively include weak identities for invalidation only."""
    if _meta_strongly_matches_board(meta, board):
        return True
    board_serial = getattr(board, "serial_number", None)
    board_location = getattr(board, "location", None)
    if meta.usb_serial is not None and board_serial is not None:
        return False
    if meta.usb_location is not None and board_location is not None:
        return False
    return meta.usb_vid is not None and (meta.usb_vid, meta.usb_pid) == (
        getattr(board, "vid", None),
        getattr(board, "pid", None),
    )


def _same_meta_identity(first, second) -> bool:
    """Return only strong cross-daemon identity matches."""
    if first.usb_serial is not None and second.usb_serial is not None:
        return first.usb_serial == second.usb_serial
    return (
        first.usb_location is not None
        and second.usb_location is not None
        and first.usb_location == second.usb_location
    )


def _meta_may_share_identity(first, second) -> bool:
    """Include weak VID/PID aliases without approving ELF association."""
    if _same_meta_identity(first, second):
        return True
    if first.usb_serial is not None and second.usb_serial is not None:
        return False
    if first.usb_location is not None and second.usb_location is not None:
        return False
    return first.usb_vid is not None and (first.usb_vid, first.usb_pid) == (
        second.usb_vid,
        second.usb_pid,
    )


def _meta_has_identity(meta) -> bool:
    return meta is not None and (
        meta.usb_serial is not None or meta.usb_location is not None or meta.usb_vid is not None
    )


def _affected_daemons(
    states: list[dict], target: dict | None, port_spec: str | None
) -> tuple[list[dict], list[dict]]:
    """Return daemons to invalidate and the confirmed subset safe to associate."""
    if target is None:
        return [], []
    try:
        board = ports.resolve_port(port_spec)
    except ports.AmbiguousPortError:
        board = None
    target_meta = _state_meta(target)
    target_confirmed = _meta_has_identity(target_meta) and (
        board is None or _meta_matches_board(target_meta, board)
    )
    target_selector = str(target.get("port", ""))
    invalidated = []
    confirmed = []
    for candidate in states:
        if candidate is target:
            invalidated.append(candidate)
            if target_confirmed:
                confirmed.append(candidate)
            continue
        candidate_meta = _state_meta(candidate)
        if (
            board is not None
            and candidate_meta is not None
            and _meta_strongly_matches_board(candidate_meta, board)
        ):
            invalidated.append(candidate)
            confirmed.append(candidate)
            continue
        if (
            board is not None
            and candidate_meta is not None
            and _meta_matches_board(candidate_meta, board)
        ):
            invalidated.append(candidate)
            continue
        if (
            target_confirmed
            and candidate_meta is not None
            and _same_meta_identity(target_meta, candidate_meta)
        ):
            invalidated.append(candidate)
            confirmed.append(candidate)
            continue
        if (
            target_confirmed
            and candidate_meta is not None
            and _meta_may_share_identity(target_meta, candidate_meta)
        ):
            invalidated.append(candidate)
            continue
        selector = str(candidate.get("port", ""))
        # Normalized bidirectional containment: "usb0" aliases "/dev/ttyUSB0"
        # in either direction, and raw "2" no longer aliases "ttyUSB20" only
        # by accident of substring position.
        spec_key = port_match_key(port_spec) if port_spec is not None else ""
        selector_key = port_match_key(selector)
        logical_alias = (
            selector == target_selector
            or selector == "auto"
            or (
                bool(spec_key)
                and bool(selector_key)
                and (spec_key in selector_key or selector_key in spec_key)
            )
        )
        if logical_alias and not _meta_has_identity(candidate_meta):
            invalidated.append(candidate)
    return invalidated, confirmed


# USB identity can gain or lose serial/topology fields across bootloader
# transitions. A single global lease avoids splitting the lock namespace
# and is intentionally conservative: flashing is rare, and correctness is
# more important than parallel programming of independent boards.
FLASH_LEASE_NAME = "all-hwlog-flashers"


def archive_elf(elf: Path, *, expected_digest: str | None = None) -> Path:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(elf, flags)
    try:
        source_stream = os.fdopen(source_fd, "rb")
        source_fd = -1
        with source_stream as source:
            source_stat = os.fstat(source.fileno())
            size = source_stat.st_size
            if not stat.S_ISREG(source_stat.st_mode):
                raise ValueError("ELF must be a regular file")
            if size <= 0 or size > MAX_ELF_BYTES:
                raise ValueError(f"ELF size must be between 1 and {MAX_ELF_BYTES} bytes")
            digest_builder = hashlib.sha256()
            while chunk := source.read(1024 * 1024):
                digest_builder.update(chunk)
            full_digest = digest_builder.hexdigest()
            if expected_digest is not None and full_digest != expected_digest:
                raise ValueError("ELF changed after it was selected")
            digest = full_digest[:12]
            stamp = time.strftime("%Y%m%d-%H%M%S")
            safe_stem = port_slug(elf.stem)[:80]
            dest = elf_archive_dir() / f"{safe_stem}-{stamp}-{digest}.elf"
            _session_limit, total_limit = configured_storage_limits()
            with storage_quota_guard():
                try:
                    dest_fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    existing_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    try:
                        existing_fd = os.open(dest, existing_flags)
                    except OSError:
                        raise OSError(f"refusing unsafe ELF archive destination: {dest}") from None
                    with os.fdopen(existing_fd, "rb") as existing:
                        existing_stat = os.fstat(existing.fileno())
                        existing_digest = hashlib.sha256()
                        while chunk := existing.read(1024 * 1024):
                            existing_digest.update(chunk)
                    if (
                        not stat.S_ISREG(existing_stat.st_mode)
                        or existing_stat.st_size != size
                        or existing_digest.hexdigest() != full_digest
                    ):
                        raise OSError(
                            f"refusing conflicting ELF archive destination: {dest}"
                        ) from None
                    return dest
                reserved, _usage, complete = reserve_global_bytes_locked(
                    size,
                    total_limit,
                    force_reconcile=True,
                    protected_elfs={dest},
                )
                if not reserved or not complete:
                    os.close(dest_fd)
                    dest.unlink(missing_ok=True)
                    raise ValueError("ELF archive exceeds the global storage limit")
                copied_digest = hashlib.sha256()
                open_dest_fd = dest_fd
                try:
                    source.seek(0)
                    target_stream = os.fdopen(open_dest_fd, "wb")
                    open_dest_fd = -1
                    with target_stream as target:
                        while chunk := source.read(1024 * 1024):
                            target.write(chunk)
                            copied_digest.update(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                    if copied_digest.hexdigest() != full_digest:
                        dest.unlink(missing_ok=True)
                        raise OSError("ELF changed while it was being archived")
                except BaseException:
                    if open_dest_fd >= 0:
                        with contextlib.suppress(OSError):
                            os.close(open_dest_fd)
                    dest.unlink(missing_ok=True)
                    release_global_bytes_locked(size)
                    raise
    finally:
        if source_fd >= 0:
            os.close(source_fd)
    return dest


def latest_archived_elf() -> Path | None:
    elfs = sorted(
        (p for p in elf_archive_dir().glob("*.elf") if p.is_file() and not p.is_symlink()),
        key=lambda p: p.stat().st_mtime,
    )
    return elfs[-1] if elfs else None


@contextmanager
def _paused_daemon(state: dict | None) -> Iterator[dict[str, bool | int | None]]:
    outcome: dict[str, bool | int | None] = {
        "firmware_may_have_changed": False,
        "firmware_generation": None,
    }
    if not state:
        yield outcome
        return
    lease = uuid.uuid4().hex
    try:
        response = daemon.send_cmd(
            state,
            {"cmd": "pause", "lease": lease, "owner_pid": os.getpid()},
            timeout=daemon.PAUSE_TIMEOUT_S + 1,
        )
    except (OSError, daemon.DaemonError):
        with contextlib.suppress(OSError, daemon.DaemonError):
            daemon.send_cmd(state, {"cmd": "resume", "lease": lease})
        raise
    if not response.get("ok"):
        with contextlib.suppress(OSError, daemon.DaemonError):
            daemon.send_cmd(state, {"cmd": "resume", "lease": lease})
        raise daemon.DaemonError(response.get("error") or "capture daemon did not release port")
    try:
        yield outcome
    finally:
        # Never leave capture paused. If another failure is already propagating,
        # retain it rather than masking it with a secondary resume failure.
        primary_error = sys.exc_info()[0] is not None
        try:
            response = daemon.send_cmd(
                state,
                {
                    "cmd": "resume",
                    "lease": lease,
                    "awaiting_elf": outcome["firmware_may_have_changed"],
                },
            )
            if not response.get("ok"):
                raise daemon.DaemonError(response.get("error") or "capture did not resume")
            if outcome["firmware_may_have_changed"]:
                generation = response.get("firmware_generation")
                if (
                    isinstance(generation, bool)
                    or not isinstance(generation, int)
                    or generation < 0
                ):
                    raise daemon.DaemonError("capture returned an invalid firmware generation")
                outcome["firmware_generation"] = generation
        except (OSError, daemon.DaemonError) as exc:
            if not primary_error:
                print(
                    f"warning: flash completed but capture did not resume: {exc}", file=sys.stderr
                )


@contextmanager
def _exclusive_flash_lease(selector: str) -> Iterator[None]:
    """Prevent overlapping flashers in this process and across processes."""
    slug = port_slug(selector)
    with _FLASH_LOCKS_GUARD:
        local_lock = _FLASH_LOCKS.setdefault(slug, threading.Lock())
    if not local_lock.acquire(blocking=False):
        raise daemon.DaemonError(f"another flash operation is already active for {selector}")
    lock = None
    fd: int | None = None
    try:
        path = daemon.daemon_dir() / f"flash-{slug}.lock"
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        os.fchmod(fd, 0o600)
        lock = os.fdopen(fd, "a")
        fd = None
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise daemon.DaemonError(
                f"another flash operation is already active for {selector}"
            ) from exc
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if lock is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                lock.close()
        local_lock.release()


def run_flash(command: list[str], port_spec: str | None = None, cwd: Path | None = None) -> int:
    """Run a flash command with the capture daemon paused around it.

    Returns the flash command's exit code. Build-then-flash commands work too —
    any ELF that appears/updates during the run gets archived.
    """
    if not command or not all(isinstance(part, str) and part for part in command):
        raise ValueError("flash command must contain non-empty string arguments")
    root = (cwd or Path.cwd()).resolve()
    states = daemon.list_daemons()
    state = _select_target_daemon(states, port_spec)
    code: int

    with _exclusive_flash_lease(FLASH_LEASE_NAME):
        before_elfs = _snapshot_elfs(root)
        affected_states, association_states = _affected_daemons(states, state, port_spec)
        with ExitStack() as pause_stack:
            # ExitStack unwinds in reverse. Enter the target last so it resumes
            # first and can capture boot output while unrelated daemons restart.
            pause_order = [candidate for candidate in states if candidate is not state]
            if state is not None:
                pause_order.append(state)
            paused = {
                id(candidate): pause_stack.enter_context(_paused_daemon(candidate))
                for candidate in pause_order
            }
            if state:
                summary = (
                    f"flash started: {Path(command[0]).name} ({len(command) - 1} args redacted)"
                )
                response = daemon.send_cmd(state, {"cmd": "note", "message": summary})
                if not response.get("ok"):
                    raise daemon.DaemonError(
                        response.get("error") or "could not record flash start"
                    )
            # Even a nonzero exit can follow an erase or partial program. Once
            # execution is attempted, the prior ELF is no longer trustworthy.
            for affected in affected_states:
                paused[id(affected)]["firmware_may_have_changed"] = True
            proc = subprocess.run(command, cwd=root, check=False)
            code = proc.returncode

            if state:
                with contextlib.suppress(OSError, daemon.DaemonError):
                    daemon.send_cmd(
                        state,
                        {
                            "cmd": "note",
                            "message": f"flash finished (exit {code}); resuming capture",
                        },
                    )

        # Capture resumes before potentially expensive discovery/hash/copy work.
        # The daemon keeps crash symbolization pending until add_elf activates the
        # new image, so early boot evidence is retained without using a stale ELF.
        if code == 0:
            selected = _find_changed_elf(root, before_elfs)
            if selected is None:
                elf = None
                expected_digest = None
            else:
                elf, expected_digest = selected
            if (
                elf is None
                and state
                and Path(command[0]).name
                in {
                    "idf.py",
                    "pio",
                    "platformio",
                }
            ):
                candidate = find_recent_elf(root, since_ts=0)
                try:
                    meta = read_meta(Path(state["session"]))
                    archived = Path(meta.elf_paths[-1]) if meta.elf_paths else None
                except (OSError, UnicodeDecodeError, RecursionError, TypeError, ValueError):
                    archived = None
                matching_digest = (
                    _matching_digest(candidate, archived)
                    if candidate is not None and archived is not None
                    else None
                )
                if matching_digest is not None:
                    elf = candidate
                    expected_digest = matching_digest
            if elf:
                try:
                    dest = archive_elf(elf, expected_digest=expected_digest)
                    if state:
                        errors = []
                        if not association_states:
                            errors.append("no daemon has a confirmed matching physical identity")
                        for affected in association_states:
                            generation = paused[id(affected)]["firmware_generation"]
                            if isinstance(generation, bool) or not isinstance(generation, int):
                                errors.append("capture did not return a firmware generation")
                                continue
                            try:
                                response = daemon.send_cmd(
                                    affected,
                                    {
                                        "cmd": "add_elf",
                                        "path": str(dest),
                                        "firmware_generation": generation,
                                    },
                                )
                            except (OSError, daemon.DaemonError) as exc:
                                errors.append(str(exc))
                                continue
                            if not response.get("ok"):
                                errors.append(
                                    response.get("error") or "daemon rejected archived ELF"
                                )
                        if errors:
                            raise daemon.DaemonError("; ".join(errors))
                except (OSError, ValueError, daemon.DaemonError) as exc:
                    print(
                        f"warning: flash succeeded but ELF archival/association failed: {exc}",
                        file=sys.stderr,
                    )
            elif state or affected_states:
                # Only worth a warning when a daemon's symbolization is left
                # stale — a plain .bin flash with no daemon running has no
                # association to miss.
                print(
                    "warning: flash succeeded but no single unambiguous ELF candidate was found; "
                    "the new firmware remains unassociated for symbolization",
                    file=sys.stderr,
                )
    return code
