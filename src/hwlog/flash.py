"""Flash-safe wrapper: pause capture, run the flash tool, resume, archive the ELF.

Two field-proven rules live here:

1. **The port owner must step aside during flashing.** esptool against a port
   held by a monitor fails in ways that look exactly like a bricked board.
2. **Archive the ELF at flash time.** A panic backtrace is only symbolizable
   against the ELF that produced it; if you only keep the latest build, the
   one crash you care about becomes undecodable the moment you rebuild.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from pathlib import Path

from . import daemon
from .session import base_dir

ELF_SEARCH_GLOBS = (
    "build/*.elf",              # ESP-IDF
    ".pio/build/*/*.elf",       # PlatformIO
    "**/*.ino.elf",             # arduino-cli export/build dirs
    "*.elf",
)
ELF_SEARCH_LIMIT = 2000  # files scanned per glob; keeps ** patterns bounded


def elf_archive_dir() -> Path:
    d = base_dir() / "elf-archive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_recent_elf(root: Path, since_ts: float) -> Path | None:
    """Newest ELF under root modified after since_ts (fresh build artifacts)."""
    candidates: list[Path] = []
    for pattern in ELF_SEARCH_GLOBS:
        for i, p in enumerate(root.glob(pattern)):
            if i > ELF_SEARCH_LIMIT:
                break
            try:
                if p.is_file() and p.stat().st_mtime >= since_ts:
                    candidates.append(p)
            except OSError:
                continue
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def archive_elf(elf: Path) -> Path:
    digest = hashlib.sha256(elf.read_bytes()).hexdigest()[:12]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = elf_archive_dir() / f"{elf.stem}-{stamp}-{digest}.elf"
    if not dest.exists():
        shutil.copy2(elf, dest)
    return dest


def latest_archived_elf() -> Path | None:
    elfs = sorted(elf_archive_dir().glob("*.elf"), key=lambda p: p.stat().st_mtime)
    return elfs[-1] if elfs else None


def run_flash(command: list[str], port_spec: str | None = None, cwd: Path | None = None) -> int:
    """Run a flash command with the capture daemon paused around it.

    Returns the flash command's exit code. Build-then-flash commands work too —
    any ELF that appears/updates during the run gets archived.
    """
    root = cwd or Path.cwd()
    state = daemon.find_daemon(port_spec)
    started = time.time()

    if state:
        daemon.send_cmd(state, {"cmd": "pause"})
        daemon.append_note(state["session"], f"flash started: {' '.join(command)}")
        time.sleep(0.5)  # let the OS release the fd before esptool opens it

    code = -1
    try:
        proc = subprocess.run(command, cwd=root, check=False)
        code = proc.returncode
    finally:
        if state:
            daemon.send_cmd(state, {"cmd": "resume"})
            daemon.append_note(state["session"], f"flash finished (exit {code}); capture resumed")

    # Archive the freshest ELF so future backtraces stay symbolizable.
    elf = find_recent_elf(root, since_ts=started - 300)  # build may predate flash slightly
    if elf:
        dest = archive_elf(elf)
        if state:
            daemon.send_cmd(state, {"cmd": "add_elf", "path": str(dest)})
    return code
