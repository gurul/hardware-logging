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
import json
import os
import re
from dataclasses import asdict
from pathlib import Path

from .crash import CrashReport
from .records import LogRecord, SessionMeta, now_iso


def base_dir() -> Path:
    return Path(os.environ.get("HWLOG_DIR", "~/.hwlog")).expanduser()


def sessions_dir() -> Path:
    return base_dir() / "sessions"


def port_slug(port: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", port).strip("-").lower()


def list_sessions() -> list[Path]:
    """Session directories, oldest first (directory name sorts chronologically)."""
    root = sessions_dir()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "meta.json").exists())


def resolve_session(selector: str | None = None) -> Path | None:
    """Resolve a session by name, or the current/latest one when selector is None."""
    if selector:
        p = sessions_dir() / selector
        return p if p.is_dir() else None
    current = base_dir() / "current"
    if current.is_symlink() and current.resolve().is_dir():
        return current.resolve()
    all_sessions = list_sessions()
    return all_sessions[-1] if all_sessions else None


def read_meta(session: Path) -> SessionMeta:
    return SessionMeta.from_json((session / "meta.json").read_text())


class SessionWriter:
    """Owned by the capture loop. Appends records with per-line flush so
    readers (and agents) see output in near-real-time."""

    def __init__(self, meta: SessionMeta) -> None:
        stamp = meta.started_at.replace(":", "").replace("-", "")[:15].replace("T", "-")
        self.dir = sessions_dir() / f"{stamp}-{port_slug(meta.port)}"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "crashes").mkdir(exist_ok=True)
        self.meta = meta
        self.meta.session_id = self.dir.name
        self._seq = 0
        self._log = (self.dir / "log.jsonl").open("a", encoding="utf-8")
        self._raw = (self.dir / "raw.log").open("ab")
        self._write_meta()
        self._point_current()

    # -- record writing ----------------------------------------------------

    @property
    def record_count(self) -> int:
        return self._seq

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def write_record(self, record: LogRecord) -> None:
        self._log.write(record.to_json() + "\n")
        self._log.flush()

    def write_raw(self, data: bytes) -> None:
        self._raw.write(data)
        self._raw.flush()

    def write_crash(self, report: CrashReport) -> Path:
        self.meta.crashes += 1
        path = self.dir / "crashes" / f"{self.meta.crashes:03d}.json"
        path.write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False))
        self._write_meta()
        return path

    def note_boot(self) -> int:
        self.meta.boots += 1
        self._write_meta()
        return self.meta.boots

    def add_elf(self, elf_path: str) -> None:
        if elf_path not in self.meta.elf_paths:
            self.meta.elf_paths.append(elf_path)
            self._write_meta()

    def close(self) -> None:
        self.meta.ended_at = now_iso()
        self._write_meta()
        self._log.close()
        self._raw.close()

    # -- internals ---------------------------------------------------------

    def _write_meta(self) -> None:
        (self.dir / "meta.json").write_text(self.meta.to_json())

    def _point_current(self) -> None:
        current = base_dir() / "current"
        with contextlib.suppress(OSError):
            if current.is_symlink() or current.exists():
                current.unlink()
            current.symlink_to(self.dir)
