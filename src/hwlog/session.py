"""Session storage: one directory per capture session.

Layout under ``$HWLOG_DIR`` (default ``~/.hwlog``)::

    sessions/<YYYYmmdd-HHMMSS>-<port-slug>/
        meta.json      # SessionMeta: port, board, ELFs, counts
        log.jsonl      # structured records, one per line (the query surface)
        raw.log        # verbatim bytes as received (the forensic surface)
        crashes/
            001.json   # full CrashReport artifacts
    current -> sessions/<...>   # symlink to the most recently started session

Everything downstream (CLI queries, MCP tools) reads these files — consumers
never touch the serial port. That separation is the whole point.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .crash import MAX_CRASH_CONTENT_CHARS, CrashReport
from .records import MAX_COUNTER, LogRecord, SessionMeta, now_iso

MAX_META_FILE_BYTES = 64 * 1024
MAX_ELF_PATHS = 256
MAX_ELF_PATH_CHARS = 4096
DEFAULT_MAX_SESSION_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_CRASH_RESERVE_BYTES = 8 * 1024 * 1024
GLOBAL_USAGE_RECHECK_BYTES = 4 * 1024 * 1024
MAX_STORAGE_SCAN_ENTRIES = 200_000
UNREFERENCED_ELF_GRACE_SECONDS = 300
_USAGE_COUNTER_BYTES = 32


def configured_storage_limits() -> tuple[int, int]:
    """Return configured per-session and global byte limits.

    Zero is a valid fail-closed setting. Invalid values are rejected instead
    of silently disabling the safety boundary.
    """

    def read_limit(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer number of bytes") from exc
        if not 0 <= value <= MAX_COUNTER:
            raise ValueError(f"{name} must be between 0 and {MAX_COUNTER}")
        return value

    return (
        read_limit("HWLOG_MAX_SESSION_BYTES", DEFAULT_MAX_SESSION_BYTES),
        read_limit("HWLOG_MAX_TOTAL_BYTES", DEFAULT_MAX_TOTAL_BYTES),
    )


def _session_stamp(started_at: str) -> str:
    """Convert a timestamp to a safe, canonical session-name component."""
    try:
        parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("session started_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("session started_at must include a timezone")
    return parsed.astimezone(UTC).strftime("%Y%m%d-%H%M%S")


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry changes where the platform supports it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            os.fsync(fd)
    except OSError:
        # Some filesystems/platforms do not support fsync on directories. The
        # rename is still atomic there, just not power-loss durable.
        pass
    finally:
        if fd >= 0:
            os.close(fd)


def _bounded_crash_dict(report: CrashReport) -> dict:
    """Keep persisted artifacts readable while prioritizing raw crash evidence."""
    remaining = MAX_CRASH_CONTENT_CHARS

    def take_text(value: str) -> str:
        nonlocal remaining
        result = value[:remaining]
        remaining -= len(result)
        return result

    def take_list(values: list[str]) -> list[str]:
        out = []
        for value in values:
            if remaining <= 0:
                break
            out.append(take_text(value))
        return out

    return {
        "crash_id": report.crash_id,
        "first_line": take_text(report.first_line),
        "lines": take_list(report.lines),
        "backtrace_addrs": take_list(report.backtrace_addrs),
        "decoded_frames": take_list(report.decoded_frames),
    }


def _read_regular_bytes(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"refusing non-regular file: {path}")
        if info.st_size > maximum:
            raise ValueError(f"file exceeds {maximum} bytes: {path}")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError(f"file exceeds {maximum} bytes: {path}")
        return data
    finally:
        if fd >= 0:
            os.close(fd)


def base_dir() -> Path:
    return Path(os.environ.get("HWLOG_DIR", "~/.hwlog")).expanduser().resolve(strict=False)


def sessions_dir() -> Path:
    return base_dir() / "sessions"


def port_slug(port: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", port).strip("-").lower()
    digest = hashlib.sha256(port.encode("utf-8", "replace")).hexdigest()[:12]
    if not slug:
        return f"unknown-{digest}"
    return f"{slug[:67]}-{digest}"


def ensure_private_dir(path: Path) -> Path:
    """Create a storage directory and enforce owner-only access."""
    if path.is_symlink():
        raise OSError(f"refusing symlinked storage directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


@contextlib.contextmanager
def storage_quota_guard():
    """Serialize quota reservations and global pruning across daemon processes."""
    root = ensure_private_dir(base_dir())
    path = root / ".storage.lock"
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    lock = None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError(f"refusing unsafe storage quota lock: {path}")
        os.fchmod(fd, 0o600)
        lock = os.fdopen(fd, "a+")
        fd = -1
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fd >= 0:
            os.close(fd)
        if lock is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


def _usage_counter_fd() -> int:
    path = base_dir() / ".storage-usage"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise OSError(f"refusing unsafe storage usage counter: {path}")
    os.fchmod(fd, 0o600)
    return fd


def _read_usage_counter_locked() -> int | None:
    fd = _usage_counter_fd()
    try:
        data = os.pread(fd, _USAGE_COUNTER_BYTES, 0)
        if not data:
            os.pwrite(fd, b"0".ljust(_USAGE_COUNTER_BYTES - 1, b" ") + b"\n", 0)
            os.ftruncate(fd, _USAGE_COUNTER_BYTES)
            os.fsync(fd)
            return None
        if len(data) != _USAGE_COUNTER_BYTES:
            return None
        value = int(data.strip())
        return value if 0 <= value <= MAX_COUNTER else None
    except ValueError:
        return None
    finally:
        os.close(fd)


def _write_usage_counter_locked(value: int) -> None:
    if not 0 <= value <= MAX_COUNTER:
        raise ValueError("storage usage counter is out of range")
    payload = str(value).encode().ljust(_USAGE_COUNTER_BYTES - 1, b" ") + b"\n"
    fd = _usage_counter_fd()
    try:
        os.pwrite(fd, payload, 0)
        os.ftruncate(fd, _USAGE_COUNTER_BYTES)
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    """Durably replace a small state file without exposing partial JSON."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def list_sessions() -> list[Path]:
    """Session directories, oldest first (directory name sorts chronologically)."""
    root = sessions_dir()
    if not root.is_dir():
        return []
    sessions = (
        p
        for p in root.iterdir()
        if p.is_dir()
        and not p.is_symlink()
        and (p / "meta.json").is_file()
        and not (p / "meta.json").is_symlink()
    )
    return sorted(sessions, key=_natural_name_key)


def _natural_name_key(path: Path) -> tuple[tuple[int, int | str], ...]:
    """Sort legacy unpadded collision suffixes numerically, not lexically."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", path.name)
        if part
    )


def resolve_session(selector: str | None = None) -> Path | None:
    """Resolve a session by name, or the current/latest one when selector is None."""
    if selector:
        # Session IDs are basenames, never paths. This prevents CLI/MCP selectors
        # from escaping into arbitrary directories via ../, absolute paths, or a
        # symlink planted below sessions/.
        if not isinstance(selector, str) or not selector or Path(selector).name != selector:
            return None
        p = sessions_dir() / selector
        if p.is_symlink() or not p.is_dir():
            return None
        try:
            p.resolve().relative_to(sessions_dir().resolve())
        except (OSError, ValueError):
            return None
        return p
    current = base_dir() / "current"
    if current.is_symlink():
        try:
            resolved = current.resolve(strict=True)
            resolved.relative_to(sessions_dir().resolve())
            if resolved.parent == sessions_dir().resolve() and resolved.is_dir():
                return resolved
        except (OSError, ValueError):
            pass
    all_sessions = list_sessions()
    return all_sessions[-1] if all_sessions else None


def read_meta(session: Path) -> SessionMeta:
    path = session / "meta.json"
    return SessionMeta.from_json(_read_regular_bytes(path, MAX_META_FILE_BYTES).decode("utf-8"))


def _tree_storage_usage(root: Path) -> tuple[int, bool]:
    """Return regular-file bytes below root and whether the scan was complete."""
    if root.is_symlink() or not root.is_dir():
        return 0, True
    total = 0
    visited = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            return total, False
        with entries:
            for entry in entries:
                visited += 1
                if visited > MAX_STORAGE_SCAN_ENTRIES:
                    return total, False
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
                elif stat.S_ISDIR(info.st_mode):
                    stack.append(Path(entry.path))
    return total, True


def storage_usage_bytes() -> int:
    """Best-effort byte usage of regular files in the hwlog-owned root."""
    total, _complete = _tree_storage_usage(base_dir())
    return total


def _safe_archive_file(path: Path) -> tuple[Path, os.stat_result] | None:
    archive = base_dir() / "elf-archive"
    if archive.is_symlink() or not archive.is_dir():
        return None
    try:
        archive_info = archive.lstat()
        archive_root = archive.resolve(strict=True)
        resolved = path.resolve(strict=True)
        info = path.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISDIR(archive_info.st_mode)
        or archive_info.st_uid != os.getuid()
        or resolved.parent != archive_root
        or resolved.suffix.lower() != ".elf"
        or path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        return None
    return resolved, info


def _referenced_archive_paths() -> tuple[set[Path], bool]:
    """Return canonical ELF references and whether every metadata file was readable."""
    references: set[Path] = set()
    complete = True
    for session in list_sessions():
        try:
            meta = read_meta(session)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            complete = False
            continue
        for value in meta.elf_paths:
            safe = _safe_archive_file(Path(value))
            if safe is not None:
                references.add(safe[0])
    return references, complete


def _prune_unreferenced_elf_paths(
    candidates: set[str] | None = None,
    *,
    protected: set[Path] | None = None,
    minimum_age_seconds: int = UNREFERENCED_ELF_GRACE_SECONDS,
) -> int:
    """Remove only unreferenced regular ELF files directly under our archive."""
    archive = base_dir() / "elf-archive"
    if archive.is_symlink() or not archive.is_dir():
        return 0
    references, complete = _referenced_archive_paths()
    # Corrupt metadata might contain a reference we cannot see. Fail closed and
    # leave the archive untouched rather than risking loss of a useful ELF.
    if not complete:
        return 0
    if candidates is None:
        try:
            entries = os.scandir(archive)
        except OSError:
            return 0
        with entries:
            paths = {Path(entry.path) for entry in entries}
    else:
        paths = {Path(value) for value in candidates}
    protected_resolved = {path.resolve(strict=False) for path in (protected or set())}
    removed = 0
    changed = False
    cutoff_ns = time.time_ns() - max(0, minimum_age_seconds) * 1_000_000_000
    for path in paths:
        safe = _safe_archive_file(path)
        if safe is None:
            continue
        resolved, info = safe
        if resolved in references or resolved in protected_resolved or info.st_mtime_ns > cutoff_ns:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed += info.st_size
        changed = True
    if changed:
        _fsync_directory(archive)
    return removed


def _safe_completed_session(path: Path, protected: set[Path]) -> bool:
    root = sessions_dir()
    try:
        root_resolved = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        info = path.lstat()
        meta = read_meta(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return False
    return (
        resolved.parent == root_resolved
        and resolved not in protected
        and not path.is_symlink()
        and stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and meta.ended_at is not None
    )


def _remove_completed_session(path: Path, protected: set[Path]) -> bool:
    if not _safe_completed_session(path, protected):
        return False
    try:
        shutil.rmtree(path)
        _fsync_directory(sessions_dir())
        return True
    except OSError:
        return False


def _prune_global_storage(
    maximum: int,
    *,
    reserve: int = 0,
    protected: set[Path] | None = None,
    protected_elfs: set[Path] | None = None,
) -> tuple[int, bool]:
    """Prune stale archives and oldest completed sessions to a global target."""
    target = max(0, maximum - max(0, reserve))
    protected_resolved = {path.resolve(strict=False) for path in (protected or set())}
    usage, complete = _tree_storage_usage(base_dir())
    if complete and usage <= target:
        return usage, complete

    _prune_unreferenced_elf_paths(protected=protected_elfs)
    usage, complete = _tree_storage_usage(base_dir())
    if complete and usage <= target:
        return usage, complete

    for session in list_sessions():
        if not _remove_completed_session(session, protected_resolved):
            continue
        _prune_unreferenced_elf_paths(protected=protected_elfs)
        usage, complete = _tree_storage_usage(base_dir())
        if complete and usage <= target:
            break

    # Session removal may have made its formerly referenced ELF stale.
    _prune_unreferenced_elf_paths(protected=protected_elfs)
    return _tree_storage_usage(base_dir())


def reconcile_global_usage_locked(
    maximum: int,
    *,
    reserve: int = 0,
    protected: set[Path] | None = None,
    protected_elfs: set[Path] | None = None,
) -> tuple[int, bool]:
    """Reconcile the shared counter with disk while holding storage_quota_guard."""
    usage, complete = _prune_global_storage(
        maximum,
        reserve=reserve,
        protected=protected,
        protected_elfs=protected_elfs,
    )
    if complete:
        _write_usage_counter_locked(usage)
    return usage, complete


def reserve_global_bytes_locked(
    amount: int,
    maximum: int,
    *,
    force_reconcile: bool = False,
    protected: set[Path] | None = None,
    protected_elfs: set[Path] | None = None,
) -> tuple[bool, int, bool]:
    """Reserve shared quota while the caller holds the guard through its write."""
    usage = _read_usage_counter_locked()
    complete = usage is not None
    if usage is None or force_reconcile or usage + amount > maximum:
        usage, complete = reconcile_global_usage_locked(
            maximum,
            reserve=amount,
            protected=protected,
            protected_elfs=protected_elfs,
        )
    if not complete or usage + amount > maximum:
        return False, usage, complete
    usage += amount
    _write_usage_counter_locked(usage)
    return True, usage, complete


def release_global_bytes_locked(amount: int) -> None:
    """Return a failed/shrunk write reservation while holding the quota guard."""
    usage = _read_usage_counter_locked()
    if usage is not None:
        _write_usage_counter_locked(max(0, usage - max(0, amount)))


class SessionWriter:
    """Owned by the capture loop. Appends records with per-line flush so
    readers (and agents) see output in near-real-time."""

    def __init__(self, meta: SessionMeta) -> None:
        # Validate caller-provided metadata before allocating any persistent paths.
        SessionMeta.from_json(meta.to_json())
        stamp = _session_stamp(meta.started_at)
        session_limit, total_limit = configured_storage_limits()
        ensure_private_dir(base_dir())
        root = ensure_private_dir(sessions_dir())
        with storage_quota_guard():
            # Materialize the fixed-size shared counter before scanning so its
            # own bytes are included in every process's common baseline.
            _read_usage_counter_locked()
            global_usage, global_scan_complete = reconcile_global_usage_locked(total_limit)
        stem = f"{stamp}-{port_slug(meta.port)}"
        for attempt in range(1000):
            suffix = "" if attempt == 0 else f"-{attempt + 1:04d}"
            candidate = root / f"{stem}{suffix}"
            if candidate.resolve(strict=False).parent != root.resolve(strict=True):
                raise ValueError("session path escapes the storage root")
            try:
                candidate.mkdir(mode=0o700)
            except FileExistsError:
                continue
            self.dir = candidate
            break
        else:  # pragma: no cover - would require 1000 starts in one second
            raise RuntimeError(f"could not allocate a unique session directory for {stem}")
        ensure_private_dir(self.dir / "crashes")
        self.meta = meta
        self.meta.session_id = self.dir.name
        self._seq = 0
        self._lock = threading.RLock()
        self._closed = False
        self._session_limit = session_limit
        self._total_limit = total_limit
        crash_reserve = min(
            session_limit,
            max(
                min(1024 * 1024, session_limit),
                min(DEFAULT_CRASH_RESERVE_BYTES, session_limit // 16),
            ),
        )
        self._log_limit = max(0, session_limit - crash_reserve)
        self._session_data_bytes = 0
        self._log_bytes = 0
        self._global_usage = global_usage
        self._global_scan_complete = global_scan_complete
        self._global_since_check = 0
        self._drop_checkpoint = (
            meta.dropped_raw_bytes // (1024 * 1024),
            meta.dropped_records // 1000,
            meta.dropped_crashes,
        )
        self.meta.storage_limit_bytes = session_limit
        self._log = (self.dir / "log.jsonl").open("a", encoding="utf-8")
        try:
            self._raw = (self.dir / "raw.log").open("ab")
        except BaseException:
            self._log.close()
            self._closed = True
            raise
        try:
            (self.dir / "log.jsonl").chmod(0o600)
            (self.dir / "raw.log").chmod(0o600)
            self._write_meta()
            self._point_current()
            with storage_quota_guard():
                self._global_usage, self._global_scan_complete = reconcile_global_usage_locked(
                    total_limit, protected={self.dir}
                )
        except BaseException:
            self._log.close()
            self._raw.close()
            self._closed = True
            raise

    # -- record writing ----------------------------------------------------

    @property
    def record_count(self) -> int:
        with self._lock:
            return self._seq

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def write_record(self, record: LogRecord) -> None:
        with self._lock:
            self._ensure_open()
            self._write_record_unlocked(record)

    def append_record(self, record: LogRecord) -> None:
        """Allocate the sequence and append atomically across capture/control threads."""
        with self._lock:
            self._ensure_open()
            self._seq += 1
            record.seq = self._seq
            self._write_record_unlocked(record)

    def write_raw(self, data: bytes) -> None:
        with self._lock:
            self._ensure_open()
            if not data:
                return
            with storage_quota_guard():
                reason = self._reserve_storage_unlocked(len(data), log_data=True)
                if reason is not None:
                    self._mark_storage_drop_unlocked(reason, raw_bytes=len(data))
                    return
                try:
                    self._raw.write(data)
                    self._raw.flush()
                except BaseException:
                    self._release_storage_unlocked(len(data), log_data=True, reconcile=True)
                    raise

    def write_crash(self, report: CrashReport) -> Path:
        with self._lock:
            self._ensure_open()
            self.meta.crashes += 1
            path = self.dir / "crashes" / f"{self.meta.crashes:03d}.json"
            payload = json.dumps(_bounded_crash_dict(report), indent=2, ensure_ascii=False)
            payload_size = len(payload.encode("utf-8"))
            with storage_quota_guard():
                reason = self._reserve_storage_unlocked(payload_size, log_data=False)
                if reason is not None:
                    self._mark_storage_drop_unlocked(reason, crash=True)
                    return path
                try:
                    atomic_write_text(path, payload)
                except BaseException:
                    self._release_storage_unlocked(payload_size, log_data=False)
                    self.meta.crashes -= 1
                    raise
                self._write_meta_unlocked()
            return path

    def update_crash(self, path: Path, report: CrashReport) -> None:
        """Atomically update a persisted artifact after background symbolization."""
        with self._lock:
            resolved = path.resolve(strict=False)
            if resolved.parent != (self.dir / "crashes").resolve(strict=False) or path.is_symlink():
                raise ValueError("crash artifact path escapes the session")
            payload = json.dumps(_bounded_crash_dict(report), indent=2, ensure_ascii=False)
            payload_size = len(payload.encode("utf-8"))
            try:
                info = path.lstat()
                previous_size = info.st_size if stat.S_ISREG(info.st_mode) else 0
            except OSError:
                previous_size = 0
            growth = max(0, payload_size - previous_size)
            with storage_quota_guard():
                reason = self._reserve_storage_unlocked(growth, log_data=False)
                if reason is not None:
                    self._mark_storage_drop_unlocked(reason)
                    return
                try:
                    atomic_write_text(resolved, payload)
                except BaseException:
                    self._release_storage_unlocked(growth, log_data=False)
                    raise
                if payload_size < previous_size:
                    self._release_storage_unlocked(previous_size - payload_size, log_data=False)

    def note_boot(self) -> int:
        with self._lock:
            self._ensure_open()
            self.meta.boots += 1
            self._write_meta_unlocked()
            return self.meta.boots

    def add_elf(
        self,
        elf_path: str,
        *,
        generation: int | None = None,
        require_pending: bool = False,
    ) -> None:
        with self._lock:
            self._ensure_open()
            if not isinstance(elf_path, str) or not elf_path or len(elf_path) > MAX_ELF_PATH_CHARS:
                raise ValueError(f"ELF path must be 1-{MAX_ELF_PATH_CHARS} characters")
            expected_generation = self.meta.firmware_generation
            if generation is not None and generation != expected_generation:
                raise ValueError("ELF belongs to a stale firmware generation")
            if require_pending and not self.meta.elf_pending:
                raise ValueError("no firmware generation is awaiting an ELF")
            archived = _safe_archive_file(Path(elf_path))
            with storage_quota_guard():
                if archived is not None and elf_path not in self.meta.elf_paths:
                    usage, complete = reconcile_global_usage_locked(
                        self._total_limit,
                        protected={self.dir},
                        protected_elfs={archived[0]},
                    )
                    self._global_usage = usage
                    self._global_scan_complete = complete
                    self._global_since_check = 0
                    if not complete or usage > self._total_limit:
                        _prune_unreferenced_elf_paths({elf_path}, minimum_age_seconds=0)
                        reconcile_global_usage_locked(self._total_limit, protected={self.dir})
                        self._mark_storage_drop_unlocked("global_limit")
                        raise ValueError("archived ELF exceeds the global storage limit")
                with contextlib.suppress(ValueError):
                    self.meta.elf_paths.remove(elf_path)
                self.meta.elf_paths.append(elf_path)
                self.meta.elf_pending = False
                self.meta.elf_generation = expected_generation
                self._write_meta_unlocked()

    def mark_flash_boundary(self, *, awaiting_elf: bool) -> int:
        with self._lock:
            self._ensure_open()
            self._log.flush()
            self.meta.wait_from_offset = os.fstat(self._log.fileno()).st_size
            if awaiting_elf:
                self.meta.firmware_generation += 1
                self.meta.elf_pending = True
            self._write_meta_unlocked()
            return self.meta.firmware_generation

    def note_board(
        self,
        *,
        vid: int | None,
        pid: int | None,
        serial_number: str | None,
        hint: str | None,
        location: str | None = None,
    ) -> None:
        """Persist the physical USB identity observed by the capture loop."""
        for name, value in (("vid", vid), ("pid", pid)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFF
            ):
                raise ValueError(f"USB {name} must be a 16-bit integer or null")
        for name, value, maximum in (
            ("serial number", serial_number, 1024),
            ("board hint", hint, 4096),
            ("USB location", location, 1024),
        ):
            if value is not None and (not isinstance(value, str) or len(value) > maximum):
                raise ValueError(f"{name} must be a string of at most {maximum} characters")
        with self._lock:
            self._ensure_open()
            self.meta.usb_vid = vid
            self.meta.usb_pid = pid
            self.meta.usb_serial = serial_number
            self.meta.usb_location = location
            self.meta.board_hint = hint
            self._write_meta_unlocked()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.meta.ended_at = now_iso()
            try:
                self._write_meta_unlocked()
            finally:
                try:
                    self._log.close()
                finally:
                    self._raw.close()
                    self._closed = True

    # -- internals ---------------------------------------------------------

    def _reserve_storage_unlocked(self, amount: int, *, log_data: bool) -> str | None:
        """Reserve bytes, returning a stable cap reason when no space remains."""
        if amount <= 0:
            return None
        if log_data and self.meta.storage_capped:
            return self.meta.storage_cap_reason or "session_limit"
        if log_data and self._log_bytes + amount > self._log_limit:
            return "session_limit"
        if self._session_data_bytes + amount > self._session_limit:
            return "session_limit"

        must_recheck = (
            not self._global_scan_complete
            or self._global_since_check >= GLOBAL_USAGE_RECHECK_BYTES
            or self._global_usage + amount > self._total_limit
        )
        reserved, usage, complete = reserve_global_bytes_locked(
            amount,
            self._total_limit,
            force_reconcile=must_recheck,
            protected={self.dir},
        )
        self._global_usage = usage
        self._global_scan_complete = complete
        if must_recheck:
            self._global_since_check = 0
        if not reserved:
            return "global_limit"

        self._session_data_bytes += amount
        self._global_since_check += amount
        if log_data:
            self._log_bytes += amount
        return None

    def _release_storage_unlocked(
        self, amount: int, *, log_data: bool, reconcile: bool = False
    ) -> None:
        if amount <= 0:
            return
        self._session_data_bytes = max(0, self._session_data_bytes - amount)
        self._global_usage = max(0, self._global_usage - amount)
        if reconcile:
            self._global_usage, self._global_scan_complete = reconcile_global_usage_locked(
                self._total_limit, protected={self.dir}
            )
            self._global_since_check = 0
        else:
            release_global_bytes_locked(amount)
        if log_data:
            self._log_bytes = max(0, self._log_bytes - amount)

    def _mark_storage_drop_unlocked(
        self,
        reason: str,
        *,
        raw_bytes: int = 0,
        record: bool = False,
        crash: bool = False,
    ) -> None:
        first = not self.meta.storage_capped
        self.meta.storage_capped = True
        self.meta.storage_cap_reason = reason
        self.meta.dropped_raw_bytes += raw_bytes
        self.meta.dropped_records += int(record)
        self.meta.dropped_crashes += int(crash)
        checkpoint = (
            self.meta.dropped_raw_bytes // (1024 * 1024),
            self.meta.dropped_records // 1000,
            self.meta.dropped_crashes,
        )
        if first or checkpoint != self._drop_checkpoint:
            self._drop_checkpoint = checkpoint
            self._write_meta_unlocked()

    def _write_meta(self) -> None:
        with self._lock:
            self._write_meta_unlocked()

    def _write_meta_unlocked(self) -> None:
        evicted: set[str] = set()
        while len(self.meta.elf_paths) > MAX_ELF_PATHS:
            evicted.add(self.meta.elf_paths.pop(0))
        payload = self.meta.to_json()
        while len(payload.encode("utf-8")) > MAX_META_FILE_BYTES and self.meta.elf_paths:
            evicted.add(self.meta.elf_paths.pop(0))
            payload = self.meta.to_json()
        if len(payload.encode("utf-8")) > MAX_META_FILE_BYTES:
            raise ValueError(f"session metadata exceeds {MAX_META_FILE_BYTES} bytes")
        atomic_write_text(self.dir / "meta.json", payload)
        if evicted:
            _prune_unreferenced_elf_paths(evicted)

    def _write_record_unlocked(self, record: LogRecord) -> None:
        payload = record.to_json() + "\n"
        payload_size = len(payload.encode("utf-8"))
        with storage_quota_guard():
            reason = self._reserve_storage_unlocked(payload_size, log_data=True)
            if reason is not None:
                self._mark_storage_drop_unlocked(reason, record=True)
                return
            try:
                self._log.write(payload)
                self._log.flush()
            except BaseException:
                self._release_storage_unlocked(payload_size, log_data=True, reconcile=True)
                raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("session writer is closed")

    def _point_current(self) -> None:
        current = base_dir() / "current"
        tmp = base_dir() / f".current.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        with contextlib.suppress(OSError):
            tmp.symlink_to(self.dir)
            os.replace(tmp, current)
            _fsync_directory(base_dir())
        tmp.unlink(missing_ok=True)
