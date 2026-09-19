import asyncio
import json

import pytest
from fastmcp import Client
from typer.testing import CliRunner

from hwlog import capabilities, mcp_server, ports, query, summary
from hwlog.capture import MAX_SEND_BYTES
from hwlog.cli import app
from hwlog.ports import Board

runner = CliRunner()


def fn(tool):
    return getattr(tool, "fn", tool)


@pytest.fixture
def boards(monkeypatch):
    rows = [
        Board("/dev/cu.a", "Control board", 0x303A, 0x1001, "A", "Espressif", "1-1"),
        Board("/dev/cu.b", "Probe", 0x2E8A, 0x1001, "B", "Raspberry Pi", "1-2"),
        Board("/dev/cu.c", "Control board", 0x303A, 0x1002, "a", "Espressif", None),
        Board("/dev/other", "Unknown", None, None, None, None, None),
    ]
    monkeypatch.setattr(ports, "discover", lambda: rows)
    return rows


def test_combined_filters_and_cli_mcp_parity(boards):
    result = runner.invoke(
        app,
        [
            "ports",
            "--match",
            "ESPRESSIF",
            "--vid",
            "0x303a",
            "--pid",
            "4097",
            "--serial",
            "A",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    cli_rows = [json.loads(line) for line in result.output.splitlines()]
    mcp_rows = fn(mcp_server.list_serial_ports)(
        match="ESPRESSIF", vid=0x303A, pid=4097, serial_number="A"
    )
    assert cli_rows == mcp_rows == [boards[0].__dict__]
    assert ports.filter_boards(boards, serial_number="a") == [boards[2]]
    assert ports.filter_boards(boards, match="1-2") == [boards[1]]
    assert ports.filter_boards(boards, match="cu.b") == [boards[1]]
    assert ports.filter_boards(boards, match="unknown") == [boards[3]]
    assert ports.filter_boards(boards, match="missing") == []
    assert ports.filter_boards(boards) == boards


def test_discovery_filters_before_output_limit(monkeypatch):
    rows = [Board(f"/dev/{i}", "board", 1, 2, str(i), None) for i in range(150)]
    monkeypatch.setattr(ports, "discover", lambda: rows)
    assert fn(mcp_server.list_serial_ports)(serial_number="149") == [rows[-1].__dict__]
    unfiltered = fn(mcp_server.list_serial_ports)()
    assert unfiltered[0]["omitted_ports"] == 50
    assert len(unfiltered) == 101


@pytest.mark.parametrize(
    "kwargs",
    [
        {"vid": -1},
        {"pid": 65536},
        {"vid": True},
        {"vid": "0x303A"},
        {"match": "x" * 1025},
        {"serial_number": 1},
    ],
)
def test_invalid_discovery_filters(kwargs):
    with pytest.raises(ValueError):
        ports.filter_boards([], **kwargs)


@pytest.mark.parametrize("value", ["-1", "65536", "not-a-vid"])
def test_invalid_cli_usb_id(value, boards):
    result = runner.invoke(app, ["ports", "--vid", value])
    assert result.exit_code == 2
    assert "error:" in result.output


@pytest.mark.parametrize("enabled", [False, True])
def test_manifest_shared_contract_and_actual_limits(monkeypatch, enabled):
    monkeypatch.setenv("HWLOG_MCP_ALLOW_SEND", "1" if enabled else "0")
    result = runner.invoke(app, ["describe"])
    assert result.exit_code == 0
    manifest = json.loads(result.output)
    assert manifest == fn(mcp_server.describe_capabilities)() == capabilities.describe()
    assert manifest["writes"]["mcp_enabled"] is enabled
    assert manifest["writes"]["device_safety_limits_enforced"] is False
    assert manifest["writes"]["device_command_schema"] is None
    assert manifest["limits"]["log_tail_records"] == query.MAX_TAIL
    assert manifest["limits"]["send_bytes_including_newline"] == MAX_SEND_BYTES
    assert manifest["limits"]["summary_scan_bytes"] == summary.MAX_SCAN_BYTES
    assert manifest["limits"]["summary_output_bytes"] == summary.MAX_OUTPUT_BYTES


def test_manifest_does_not_enable_writes(monkeypatch):
    monkeypatch.delenv("HWLOG_MCP_ALLOW_SEND", raising=False)
    assert fn(mcp_server.describe_capabilities)()["writes"]["mcp_enabled"] is False
    assert fn(mcp_server.send_to_device)("move")["ok"] is False
    monkeypatch.setenv("HWLOG_MCP_ALLOW_SEND", "1")
    monkeypatch.setattr(mcp_server.daemon, "find_daemon", lambda _port: {"port": "test"})
    sent = []
    monkeypatch.setattr(
        mcp_server.daemon, "send_cmd", lambda state, command: sent.append(command) or {"ok": True}
    )
    assert fn(mcp_server.send_to_device)("move")["ok"] is True
    assert sent == [{"cmd": "send", "data": "move", "newline": True}]


def test_status_selects_device_and_refuses_ambiguity(monkeypatch):
    rows = [
        {"pid": 1, "port": "/dev/a", "session": "/sessions/a", "instance": "secret-a"},
        {"pid": 2, "port": "/dev/b", "session": "/sessions/b", "instance": "secret-b"},
    ]
    monkeypatch.setattr(mcp_server.daemon, "list_daemons", lambda: rows)
    calls = []
    monkeypatch.setattr(
        mcp_server.daemon, "send_cmd", lambda state, command: calls.append(state) or {"ok": True}
    )
    assert fn(mcp_server.capture_status)(port="/dev/")["ambiguous"] is True
    assert calls == []
    result = fn(mcp_server.capture_status)(port="/dev/b")
    assert result["session"] == "/sessions/b"
    assert calls == [rows[1]]
    assert "secret-b" not in json.dumps(result)
    assert fn(mcp_server.capture_status)(port="missing")["running"] is False


def test_mcp_transport_exposes_manifest_discovery_and_summary(make_session, boards):
    session = make_session([("E", "app", "sensor timeout")])

    async def exercise():
        async with Client(mcp_server.mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert {"describe_capabilities", "summarize_session"} <= tools.keys()
            manifest = await client.call_tool("describe_capabilities", {})
            assert manifest.data["schema_version"] == 1
            assert all(row["mcp"] in tools for row in manifest.data["observations"])
            result = await client.call_tool("summarize_session", {"session": session.name})
            assert result.data["counts"]["errors"] == 1
            result = await client.call_tool("list_serial_ports", {"serial_number": "B"})
            assert result.data[0]["device"] == boards[1].device

    asyncio.run(exercise())
