"""hwlog CLI — capture side and query side of the agent debug loop.

Query commands print compact text by default and NDJSON with ``--json``:
stable, grep-able, bounded. Exit codes are part of the contract (``wait``
returns 0 on pattern match, 1 on timeout) so agents and CI can branch on them.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import typer

from . import __version__, daemon, query
from . import flash as flash_mod
from . import ports as ports_mod
from .capture import CaptureLoop
from .records import SessionMeta, now_iso
from .session import SessionWriter, list_sessions, read_meta, resolve_session

app = typer.Typer(
    name="hwlog",
    help="Structured, crash-aware serial logging for embedded boards — built for AI coding agents.",
    no_args_is_help=True,
    add_completion=False,
)


def _fail(msg: str, code: int = 2) -> None:
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(code)


def _session_or_fail(session: str | None) -> Path:
    s = resolve_session(session)
    if s is None:
        _fail("no capture session found — start one with `hwlog start` or `hwlog monitor`")
    return s


@app.command()
def version() -> None:
    """Print version."""
    typer.echo(__version__)


@app.command("ports")
def ports_cmd(json_out: bool = typer.Option(False, "--json", help="NDJSON output")) -> None:
    """List serial ports, likely dev boards first."""
    boards = ports_mod.discover()
    if json_out:
        for b in boards:
            typer.echo(json.dumps(b.__dict__))
        return
    if not boards:
        typer.echo("no serial ports found")
        return
    for b in boards:
        marker = "●" if b.is_known else " "
        hint = f"  [{b.hint}]" if b.hint else ""
        typer.echo(f"{marker} {b.device}  {b.description}{hint}")


@app.command()
def monitor(
    port: str | None = typer.Option(None, "--port", "-p", help="Port path or substring"),
    baud: int = typer.Option(115200, "--baud", "-b"),
) -> None:
    """Foreground capture: live view + full session recording. Ctrl-C to stop."""
    board = ports_mod.resolve_port(port)
    meta = SessionMeta(
        session_id="",
        port=port or (board.device if board else "auto"),
        baud=baud,
        started_at=now_iso(),
        usb_vid=board.vid if board else None,
        usb_pid=board.pid if board else None,
        board_hint=board.hint if board else None,
    )
    writer = SessionWriter(meta)
    typer.echo(f"session: {writer.dir}", err=True)
    loop = CaptureLoop(
        writer,
        port_spec=port,
        baud=baud,
        on_line=lambda r: typer.echo(query.format_record(r)),
    )
    try:
        loop.run()
    except KeyboardInterrupt:
        loop.stop()


@app.command()
def start(
    port: str | None = typer.Option(None, "--port", "-p"),
    baud: int = typer.Option(115200, "--baud", "-b"),
) -> None:
    """Start the background capture daemon (single owner of the port)."""
    state = daemon.start_background(port, baud)
    typer.echo(f"daemon pid {state['pid']} on {state['port']} → {state['session']}")


@app.command()
def stop(port: str | None = typer.Option(None, "--port", "-p")) -> None:
    """Stop the background capture daemon."""
    state = daemon.find_daemon(port)
    if not state:
        _fail("no running daemon")
    ok = daemon.stop_daemon(state)
    typer.echo("stopped" if ok else "daemon did not exit cleanly", err=not ok)
    raise typer.Exit(0 if ok else 1)


@app.command()
def status(json_out: bool = typer.Option(False, "--json")) -> None:
    """Daemon and session status."""
    daemons = daemon.list_daemons()
    if not daemons:
        typer.echo('{"running": false}' if json_out else "no daemon running")
        return
    for state in daemons:
        try:
            resp = daemon.send_cmd(state, {"cmd": "status"})
        except OSError as e:
            resp = {"ok": False, "error": str(e)}
        info = {**state, **resp}
        if json_out:
            typer.echo(json.dumps(info))
        else:
            conn = "connected" if resp.get("connected") else "disconnected"
            paused = " (paused)" if resp.get("paused") else ""
            typer.echo(
                f"pid {state['pid']}  {state['port']}  {conn}{paused}  "
                f"boots={resp.get('boots')} crashes={resp.get('crashes')} "
                f"records={resp.get('records')}\n  session: {state['session']}"
            )


@app.command()
def logs(
    tail: int = typer.Option(query.DEFAULT_TAIL, "--tail", "-n", help="Max lines returned"),
    boot: int | None = typer.Option(None, "--boot", help="Boot index; -1 = latest boot"),
    level: str | None = typer.Option(None, "--level", "-l", help="Min severity: E, W, I, D, V"),
    grep: str | None = typer.Option(None, "--grep", "-g", help="Substring filter"),
    tag: str | None = typer.Option(None, "--tag", help="Exact tag filter"),
    since: str | None = typer.Option(None, "--since", help="ISO timestamp lower bound"),
    session: str | None = typer.Option(None, "--session", "-s"),
    no_collapse: bool = typer.Option(False, "--no-collapse", help="Keep repeated lines"),
    json_out: bool = typer.Option(False, "--json", help="NDJSON records"),
) -> None:
    """Query captured logs (bounded — safe to call from an agent loop)."""
    s = _session_or_fail(session)
    b = query.latest_boot(s) if boot == -1 else boot
    recs = query.filter_records(
        query.iter_records(s), boot=b, level=level, grep=grep, tag=tag, since=since
    )
    if not no_collapse:
        recs = query.collapse_repeats(recs)
    out = query.tail(recs, tail)
    for r in out:
        typer.echo(r.to_json() if json_out else query.format_record(r))
    if not out:
        typer.echo("(no matching records)", err=True)


@app.command()
def boots(
    session: str | None = typer.Option(None, "--session", "-s"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List boot cycles: when the device reset, and how each boot went."""
    s = _session_or_fail(session)
    rows = query.list_boots(s)
    for row in rows:
        if json_out:
            typer.echo(json.dumps(row))
        else:
            typer.echo(
                f"boot {row['boot']}: {row['started']}  lines={row['lines']} "
                f"errors={row['errors']} crashes={row['crashes']}"
            )
    if not rows:
        typer.echo("(no records)", err=True)


@app.command()
def crashes(
    session: str | None = typer.Option(None, "--session", "-s"),
    last: bool = typer.Option(False, "--last", help="Full artifact of the most recent crash"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List crash reports; --last prints the full decoded artifact."""
    s = _session_or_fail(session)
    reports = query.list_crashes(s)
    if not reports:
        typer.echo("(no crashes recorded)", err=True)
        return
    if last:
        r = reports[-1]
        if json_out:
            typer.echo(json.dumps(r, indent=2))
            return
        typer.echo(f"crash {r['crash_id']}: {r['first_line']}")
        typer.echo("--- raw ---")
        typer.echo("\n".join(r["lines"]))
        if r.get("decoded_frames"):
            typer.echo("--- decoded backtrace ---")
            typer.echo("\n".join(r["decoded_frames"]))
        elif r.get("backtrace_addrs"):
            typer.echo(
                "--- backtrace not decoded (no archived ELF or addr2line; "
                "flash via `hwlog flash` to archive ELFs) ---"
            )
            typer.echo(" ".join(r["backtrace_addrs"]))
        return
    for r in reports:
        summary = f"{r['first_line']}"
        decoded = " [decoded]" if r.get("decoded_frames") else ""
        typer.echo(f"{r['crash_id']:>3}  {summary}{decoded}")


@app.command()
def send(
    text: str = typer.Argument(..., help="Line to send to the device"),
    port: str | None = typer.Option(None, "--port", "-p"),
    no_newline: bool = typer.Option(False, "--no-newline"),
) -> None:
    """Send a line to the device through the daemon (stimulus injection)."""
    state = daemon.find_daemon(port)
    if not state:
        _fail("no running daemon — start one with `hwlog start`")
    resp = daemon.send_cmd(state, {"cmd": "send", "data": text, "newline": not no_newline})
    if not resp.get("ok"):
        _fail(resp.get("error") or "send failed", code=1)
    typer.echo("sent")


@app.command()
def wait(
    pattern: str = typer.Option(..., "--pattern", "-P", help="Regex to wait for"),
    timeout: float = typer.Option(30.0, "--timeout", "-t", help="Seconds"),
    session: str | None = typer.Option(None, "--session", "-s"),
    from_start: bool = typer.Option(
        False, "--from-start", help="Scan existing records too, not just new output"
    ),
) -> None:
    """Behavioral assertion: block until PATTERN appears in device output.

    Exit 0 = matched (prints the matching record), 1 = timeout. "It flashed"
    is not "it works" — gate your loop on observed behavior.
    """
    s = _session_or_fail(session)
    rx = re.compile(pattern)
    log_path = s / "log.jsonl"
    offset = 0 if from_start else (log_path.stat().st_size if log_path.exists() else 0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            with log_path.open(encoding="utf-8") as f:
                f.seek(offset)
                chunk = f.read()
                offset = f.tell()
            for line in chunk.splitlines():
                try:
                    rec = query.LogRecord.from_json(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if rx.search(rec.msg):
                    typer.echo(query.format_record(rec))
                    raise typer.Exit(0)
        time.sleep(0.2)
    typer.echo(f"timeout after {timeout}s waiting for /{pattern}/", err=True)
    raise typer.Exit(1)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def flash(
    ctx: typer.Context,
    port: str | None = typer.Option(None, "--port", "-p"),
) -> None:
    """Run a flash command with capture paused around it: `hwlog flash -- idf.py flash`.

    Pauses the daemon (so esptool can take the port), runs the command, resumes
    capture, and archives any fresh ELF so future backtraces stay symbolizable.
    """
    if not ctx.args:
        _fail("usage: hwlog flash -- <flash command>")
    code = flash_mod.run_flash(list(ctx.args), port_spec=port)
    raise typer.Exit(code)


@app.command()
def sessions(json_out: bool = typer.Option(False, "--json")) -> None:
    """List capture sessions, oldest first."""
    found = list_sessions()
    if not found:
        typer.echo("(no sessions)", err=True)
        return
    for p in found:
        meta = read_meta(p)
        if json_out:
            typer.echo(meta.to_json().replace("\n", " "))
        else:
            typer.echo(
                f"{meta.session_id}  port={meta.port} boots={meta.boots} "
                f"crashes={meta.crashes}{'' if meta.ended_at else '  (active)'}"
            )


@app.command()
def mcp() -> None:
    """Run the MCP server (stdio) so agents get hwlog as native tools."""
    from .mcp_server import mcp as server

    server.run()


@app.command()
def init(
    path: Path = typer.Option(Path("."), "--path", help="Project root to install into"),
) -> None:
    """Install the bundled agent skill + CLAUDE.md snippet into a project.

    Expert knowledge is as load-bearing as the plumbing: this ships a debug
    playbook (crash signatures, the capture→flash→wait loop protocol) that
    coding agents pick up automatically.
    """
    assets = Path(__file__).parent / "assets"
    skill_src = assets / "SKILL.md"
    skill_dst = path / ".claude" / "skills" / "hardware-logging" / "SKILL.md"
    skill_dst.parent.mkdir(parents=True, exist_ok=True)
    skill_dst.write_text(skill_src.read_text())
    typer.echo(f"installed skill: {skill_dst}")
    typer.echo("\nAdd this to your project's CLAUDE.md / AGENTS.md:\n")
    typer.echo((assets / "CLAUDE_SNIPPET.md").read_text())


@app.command("_daemon", hidden=True)
def _daemon_cmd(
    port: str | None = typer.Option(None, "--port", "-p"),
    baud: int = typer.Option(115200, "--baud", "-b"),
) -> None:
    """(internal) daemon entry point — use `hwlog start` instead."""
    daemon.run_daemon(port, baud)


def main() -> None:  # pragma: no cover - console_script shim
    app()


if __name__ == "__main__":
    sys.exit(app())
