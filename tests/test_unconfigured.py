from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.client import Client
from test_server import TOOL_NAMES
from test_setup import STORAGE_VARS

from mindin import cli
from mindin.server import build_server
from mindin.vault import Unconfigured, Vault, VaultResolver


def forget_the_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in STORAGE_VARS:
        monkeypatch.setenv(name, "")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_" + name, "")


@pytest.fixture
def nothing_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    forget_the_vault(monkeypatch)


def served_by_mindin(monkeypatch: pytest.MonkeyPatch) -> Vault | VaultResolver:
    served: list[Vault | VaultResolver] = []
    monkeypatch.setattr(cli, "run_stdio", served.append)
    assert cli.main([]) == 0
    return served[0]


@pytest.mark.anyio
async def test_the_server_without_a_vault_lists_the_same_tools_as_with_one(vault, monkeypatch):
    forget_the_vault(monkeypatch)
    resolver = served_by_mindin(monkeypatch)
    assert isinstance(resolver, Unconfigured)
    async with Client(build_server(resolver)) as unconfigured, Client(build_server(vault)) as configured:
        bare = (await unconfigured.list_tools()).tools
        real = (await configured.list_tools()).tools
    assert {tool.name for tool in bare} == TOOL_NAMES
    assert {tool.name: tool.input_schema for tool in bare} == {tool.name: tool.input_schema for tool in real}


@pytest.mark.anyio
async def test_every_tool_call_without_a_vault_says_how_to_set_one_up(nothing_configured, monkeypatch):
    async with Client(build_server(served_by_mindin(monkeypatch))) as client:
        results = [await client.call_tool(name, {"query": "x"} if name == "search" else {}) for name in TOOL_NAMES]
    assert all(result.is_error for result in results)
    assert all("No vault is set up. Run `mindin setup`" in result.content[0].text for result in results)
    assert not Path(os.environ["VAULT_CACHE_DIR"]).exists()


def test_the_instructions_without_a_vault_open_with_the_setup_line():
    assert build_server(Unconfigured("No vault is set up.")).instructions.startswith("No vault is set up.\n\n")


def stdio_exchange(home: Path, messages: list[dict]) -> dict[int, dict]:
    environment = {"PATH": os.environ["PATH"], "HOME": str(home)}
    server = subprocess.Popen(
        [sys.executable, "-m", "mindin.cli"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=environment,
        cwd=home,
    )
    replies: dict[int, dict] = {}
    try:
        for message in messages:
            server.stdin.write(json.dumps(message) + "\n")
            server.stdin.flush()
            if "id" in message:
                reply = json.loads(server.stdout.readline())
                replies[reply["id"]] = reply
    finally:
        server.stdin.close()
        server.wait(timeout=30)
    return replies


SEARCH = {"name": "search", "arguments": {"query": "x"}}
HELLO = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "probe", "version": "0"}}


def test_mindin_over_stdio_without_a_vault_answers_and_writes_nothing(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    by_id = stdio_exchange(
        home,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": HELLO},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": SEARCH},
        ],
    )
    assert by_id[1]["result"]["serverInfo"]["name"] == "mindin"
    assert by_id[2]["result"] == {}
    assert {tool["name"] for tool in by_id[3]["result"]["tools"]} == TOOL_NAMES
    assert by_id[4]["result"]["isError"] is True
    assert "mindin setup" in by_id[4]["result"]["content"][0]["text"]
    assert list(home.rglob("*")) == []
