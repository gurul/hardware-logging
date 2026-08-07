"""hwlog CLI — capture side and query side of the agent debug loop.

Query commands print compact text by default and NDJSON with ``--json``:
stable, grep-able, bounded. Exit codes are part of the contract (``wait``
returns 0 on pattern match, 1 on timeout) so agents and CI can branch on them.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer

from . import __version__, daemon, query
from . import flash as flash_mod
from . import ports as ports_mod
from .capture import CaptureLoop
from .parsers import strip_ansi
from .records import SessionMeta, now_iso
from .safety import MAX_WAIT_SECONDS, PatternError
from .session import SessionWriter, list_sessions, read_meta, resolve_session

app = typer.Typer(
    name="hwlog",
    help="Structured, crash-aware serial logging for embedded boards — built for AI coding agents.",
    no_args_is_help=True,
    add_completion=False,
)

MAX_CLI_CRASH_ROWS = 500
MAX_CLI_SUMMARY_CHARS = 512
MAX_CLI_BOOT_ROWS = 500


def _fail(msg: str, code: int = 2) -> None:
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(code)


def _session_or_fail(session: str | None) -> Path:
    s = resolve_session(session)
    if s is None:
        _fail("no capture session found — start one with `hwlog start` or `hwlog monitor`")
    return s


def _scan_budget(value: int | None) -> int:
    try:
        budget = query.configured_cli_scan_bytes() if value is None else value
    except ValueError as exc:
        _fail(str(exc))
    if budget < 0:
        _fail("scan bytes must be non-negative")
    return budget


def _warn_truncated_scan(session: Path, budget: int) -> None:
    if query.record_scan_is_truncated(session, budget):
        typer.echo(
            f"warning: scanned only the newest {budget} bytes; older matches were omitted "
            "(adjust --scan-bytes or HWLOG_QUERY_SCAN_BYTES)",
            err=True,
        )


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
        hint = f"  [{strip_ansi(b.hint)}]" if b.hint else ""
        typer.echo(f"{marker} {strip_ansi(b.device)}  {strip_ansi(b.description)}{hint}")


@app.command()
def monitor(
    port: str | None = typer.Option(None, "--port", "-p", help="Port path or substring"),
    baud: int = typer.Option(115200, "--baud", "-b"),
) -> None:
    """Foreground capture: live view + full session recording. Ctrl-C to stop."""
    if baud <= 0:
        _fail("baud must be positive")
    try:
        board = ports_mod.resolve_port(port)
    except ports_mod.AmbiguousPortError as exc:
        _fail(str(exc))
    meta = SessionMeta(
        session_id="",
        port=port or (board.device if board else "auto"),
        baud=baud,
        started_at=now_iso(),
        usb_vid=board.vid if board else None,
        usb_pid=board.pid if board else None,
        usb_serial=board.serial_number if board else None,
        usb_location=board.location if board else None,
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
    if baud <= 0:
        _fail("baud must be positive")
    try:
        state = daemon.start_background(port, baud)
    except (daemon.DaemonError, ValueError, RuntimeError) as exc:
        _fail(str(exc), code=1)
    typer.echo(f"daemon pid {state['pid']} on {state['port']} → {state['session']}")


@app.command()
def stop(port: str | None = typer.Option(None, "--port", "-p")) -> None:
    """Stop the background capture daemon."""
    try:
        state = daemon.find_daemon(port)
    except daemon.AmbiguousDaemonError as exc:
        _fail(str(exc))
    if not state:
        _fail("no running daemon")
    if state.get("legacy") is True:
        _fail(
            "legacy daemon control is unauthenticated; stop it with the previously installed "
            "hwlog version, or verify and terminate its recorded PID, then retry",
            code=1,
        )
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
        except (OSError, daemon.DaemonError) as e:
            resp = {"ok": False, "error": str(e)}
        public_state = {key: value for key, value in state.items() if key != "instance"}
        info = {**public_state, **resp}
        try:
            meta = read_meta(Path(state["session"]))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            meta = None
        if meta is not None:
            info.update(
                {
                    "storage_capped": meta.storage_capped,
                    "storage_cap_reason": meta.storage_cap_reason,
                    "storage_limit_bytes": meta.storage_limit_bytes,
                    "dropped_raw_bytes": meta.dropped_raw_bytes,
                    "dropped_records": meta.dropped_records,
                    "dropped_crashes": meta.dropped_crashes,
                }
            )
        if json_out:
            typer.echo(json.dumps(info))
        else:
            conn = "connected" if resp.get("connected") else "disconnected"
            paused = " (paused)" if resp.get("paused") else ""
            storage = ""
            if meta is not None:
                storage = f"\n  storage limit={meta.storage_limit_bytes}B"
                if meta.storage_capped:
                    storage += (
                        f" capped ({meta.storage_cap_reason}); "
                        f"dropped raw={meta.dropped_raw_bytes}B records={meta.dropped_records} "
                        f"crashes={meta.dropped_crashes}"
                    )
            typer.echo(
                f"pid {state['pid']}  {strip_ansi(str(state['port']))}  {conn}{paused}  "
                f"boots={resp.get('boots')} crashes={resp.get('crashes')} "
                f"records={resp.get('records')}\n  session: {strip_ansi(str(state['session']))}"
                f"{storage}"
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
    scan_bytes: int | None = typer.Option(
        None,
        "--scan-bytes",
        help="Newest log bytes to inspect (default: HWLOG_QUERY_SCAN_BYTES or 64 MiB)",
    ),
) -> None:
    """Query captured logs (bounded — safe to call from an agent loop)."""
    if tail < 0:
        _fail("tail must be non-negative")
    if level is not None and level.upper() not in query.LEVEL_ORDER:
        _fail("level must be one of E, W, I, D, V")
    if since is not None:
        try:
            parsed_since = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if parsed_since.tzinfo is None:
                raise ValueError
            since = (
                parsed_since.astimezone(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        except ValueError:
            _fail("since must be a timezone-aware ISO-8601 timestamp")
    s = _session_or_fail(session)
    budget = _scan_budget(scan_bytes)
    b = query.latest_boot(s, max_bytes=budget) if boot == -1 else boot
    recs = query.filter_records(
        query.iter_records(s, max_bytes=budget),
        boot=b,
        level=level,
        grep=grep,
        tag=tag,
        since=since,
    )
    if not no_collapse:
        recs = query.collapse_repeats(recs)
    out = query.tail(recs, tail)
    for r in out:
        typer.echo(r.to_json(ensure_ascii=True) if json_out else query.format_record(r))
    if not out:
        typer.echo("(no matching records)", err=True)
    _warn_truncated_scan(s, budget)


@app.command()
def boots(
    session: str | None = typer.Option(None, "--session", "-s"),
    json_out: bool = typer.Option(False, "--json"),
    scan_bytes: int | None = typer.Option(
        None,
        "--scan-bytes",
        help="Newest log bytes to inspect (default: HWLOG_QUERY_SCAN_BYTES or 64 MiB)",
    ),
) -> None:
    """List boot cycles: when the device reset, and how each boot went."""
    s = _session_or_fail(session)
    budget = _scan_budget(scan_bytes)
    rows = query.list_boots(s, max_bytes=budget)
    if len(rows) > MAX_CLI_BOOT_ROWS:
        typer.echo(
            f"warning: showing only the newest {MAX_CLI_BOOT_ROWS} boot rows",
            err=True,
        )
        rows = rows[-MAX_CLI_BOOT_ROWS:]
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
    _warn_truncated_scan(s, budget)


@app.command()
def crashes(
    session: str | None = typer.Option(None, "--session", "-s"),
    last: bool = typer.Option(False, "--last", help="Full artifact of the most recent crash"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List crash reports; --last prints the full decoded artifact."""
    s = _session_or_fail(session)
    if last:
        r = query.get_crash(s)
        if r is None:
            typer.echo("(no crashes recorded)", err=True)
            return
        if json_out:
            typer.echo(json.dumps(r, ensure_ascii=True))
            return
        typer.echo(f"crash {r['crash_id']}: {strip_ansi(str(r['first_line']))}")
        typer.echo("--- raw ---")
        typer.echo("\n".join(strip_ansi(str(line)) for line in r["lines"]))
        if r.get("decoded_frames"):
            typer.echo("--- decoded backtrace ---")
            typer.echo("\n".join(strip_ansi(str(line)) for line in r["decoded_frames"]))
        elif r.get("backtrace_addrs"):
            typer.echo(
                "--- backtrace not decoded (no archived ELF or addr2line; "
                "flash via `hwlog flash` to archive ELFs) ---"
            )
            typer.echo(" ".join(r["backtrace_addrs"]))
        return
    reports = query.list_crashes(s, limit=MAX_CLI_CRASH_ROWS)
    if not reports:
        typer.echo("(no crashes recorded)", err=True)
        return
    for r in reports:
        if json_out:
            typer.echo(
                json.dumps(
                    {
                        "crash_id": r["crash_id"],
                        "first_line": strip_ansi(str(r["first_line"]))[:MAX_CLI_SUMMARY_CHARS],
                        "backtrace_count": len(r.get("backtrace_addrs", [])),
                        "decoded": bool(r.get("decoded_frames")),
                    },
                    ensure_ascii=False,
                )
            )
            continue
        summary = strip_ansi(str(r["first_line"]))[:MAX_CLI_SUMMARY_CHARS]
        decoded = " [decoded]" if r.get("decoded_frames") else ""
        typer.echo(f"{r['crash_id']:>3}  {summary}{decoded}")


@app.command()
def send(
    text: str = typer.Argument(..., help="Line to send to the device"),
    port: str | None = typer.Option(None, "--port", "-p"),
    no_newline: bool = typer.Option(False, "--no-newline"),
) -> None:
    """Send a line to the device through the daemon (stimulus injection)."""
    try:
        state = daemon.find_daemon(port)
    except daemon.AmbiguousDaemonError as exc:
        _fail(str(exc))
    if not state:
        _fail("no running daemon — start one with `hwlog start`")
    try:
        resp = daemon.send_cmd(state, {"cmd": "send", "data": text, "newline": not no_newline})
    except (OSError, daemon.DaemonError) as exc:
        _fail(str(exc), code=1)
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
    try:
        rec = query.wait_for_record(s, pattern, timeout, from_start=from_start)
    except (PatternError, ValueError) as exc:
        _fail(str(exc))
    if rec is not None:
        typer.echo(query.format_record(rec))
        raise typer.Exit(0)
    bounded = min(timeout, MAX_WAIT_SECONDS)
    typer.echo(f"timeout after {bounded}s waiting for /{strip_ansi(pattern)}/", err=True)
    raise typer.Exit(1)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
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
    try:
        code = flash_mod.run_flash(list(ctx.args), port_spec=port)
    except (OSError, ValueError, daemon.DaemonError) as exc:
        _fail(str(exc), code=1)
    raise typer.Exit(code)


@app.command()
def sessions(json_out: bool = typer.Option(False, "--json")) -> None:
    """List capture sessions, oldest first."""
    found = list_sessions()
    if not found:
        typer.echo("(no sessions)", err=True)
        return
    for p in found:
        try:
            meta = read_meta(p)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            typer.echo(f"warning: skipping corrupt session metadata: {p.name}", err=True)
            continue
        if json_out:
            typer.echo(meta.to_json().replace("\n", " "))
        else:
            storage = ""
            if meta.storage_capped:
                storage = (
                    f"  [storage capped: {meta.storage_cap_reason}; "
                    f"dropped {meta.dropped_raw_bytes}B/{meta.dropped_records} records/"
                    f"{meta.dropped_crashes} crashes]"
                )
            typer.echo(
                f"{strip_ansi(meta.session_id)}  port={strip_ansi(meta.port)} boots={meta.boots} "
                f"crashes={meta.crashes}{'' if meta.ended_at else '  (active)'}{storage}"
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
