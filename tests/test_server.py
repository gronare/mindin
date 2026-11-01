from __future__ import annotations

import pytest
from mcp.client import Client

from notes_vault_mcp.server import build_server
from notes_vault_mcp.vault import Vault

TOOL_NAMES = {
    "search",
    "read_file",
    "write_file",
    "append_file",
    "move_file",
    "delete_file",
    "list_files",
    "set_status",
    "close",
    "backlog_add",
    "backlog",
    "log_append",
    "context",
    "lint",
    "init",
}


async def text_of(client: Client, name: str, arguments: dict) -> str:
    result = await client.call_tool(name, arguments)
    return "".join(block.text for block in result.content if getattr(block, "text", None))


@pytest.mark.anyio
async def test_the_server_exposes_exactly_the_designed_tools(vault: Vault):
    async with Client(build_server(vault)) as client:
        tools = await client.list_tools()
    assert {tool.name for tool in tools.tools} == TOOL_NAMES


@pytest.mark.anyio
async def test_every_tool_description_states_its_cost(vault: Vault):
    async with Client(build_server(vault)) as client:
        tools = await client.list_tools()
    assert all(tool.description.startswith(("CHEAP", "MODERATE", "WRITE")) for tool in tools.tools)


@pytest.mark.anyio
async def test_the_instructions_carry_the_schema(vault: Vault):
    server = build_server(vault)
    assert "Areas/" in server.instructions
    assert "status is one of: draft, active, backlog, complete, superseded" in server.instructions


@pytest.mark.anyio
async def test_search_returns_rendered_rows(vault: Vault):
    async with Client(build_server(vault)) as client:
        text = await text_of(client, "search", {"query": "orchard", "limit": 3})
    assert text.startswith("3 of 5 (archive: 2 hidden, superseded: 0 hidden)")


@pytest.mark.anyio
async def test_write_then_read_round_trips(vault: Vault):
    content = (
        "---\ntitle: Via MCP\ndate: 2026-08-01\nupdated: 2026-08-01\n"
        'tags: [orchard]\nstatus: active\narea: "[[orchard]]"\n---\n\nKropp.\n'
    )
    async with Client(build_server(vault)) as client:
        written = await text_of(client, "write_file", {"path": "Projects/via-mcp.md", "content": content})
        read = await text_of(client, "read_file", {"path": "Projects/via-mcp.md"})
    assert written.startswith("Written: Projects/via-mcp.md")
    assert "title: Via MCP" in read


@pytest.mark.anyio
async def test_write_file_refuses_the_schema_and_bases_but_read_file_serves_them(vault: Vault, vault_dir):
    (vault_dir / ".vault").mkdir(exist_ok=True)
    (vault_dir / ".vault" / "schema.yml").write_text("version: 1\n", encoding="utf-8")
    (vault_dir / "Areas.base").write_text("views: []\n", encoding="utf-8")
    async with Client(build_server(vault)) as client:
        schema = await client.call_tool("write_file", {"path": ".vault/schema.yml", "content": "version: 2\n"})
        base = await client.call_tool("write_file", {"path": "Areas.base", "content": "views: [x]\n"})
        read_schema = await text_of(client, "read_file", {"path": ".vault/schema.yml"})
        read_base = await text_of(client, "read_file", {"path": "Areas.base"})
    assert schema.is_error and base.is_error
    assert read_schema.split("\n", 1)[1] == "version: 1\n"
    assert read_base.split("\n", 1)[1] == "views: []\n"


@pytest.mark.anyio
async def test_a_rejected_write_reports_the_problem(vault: Vault):
    content = "---\ntitle: Utan area\ndate: 2026-08-01\nupdated: 2026-08-01\ntags: [x]\nstatus: active\n---\n\nKropp.\n"
    async with Client(build_server(vault)) as client:
        result = await client.call_tool("write_file", {"path": "Projects/utan-area.md", "content": content})
    assert result.is_error
    assert "missing area" in "".join(block.text for block in result.content)


@pytest.mark.anyio
async def test_context_and_lint_return_text(vault: Vault):
    async with Client(build_server(vault)) as client:
        context = await text_of(client, "context", {"repo": "orchard"})
        findings = await text_of(client, "lint", {})
    assert "## open tasks" in context
    assert "## stale_active" in findings


@pytest.mark.anyio
async def test_init_keeps_what_exists_and_writes_the_rest(vault: Vault):
    async with Client(build_server(vault)) as client:
        first = await text_of(client, "init", {})
        forced = await text_of(client, "init", {"force": True})
    assert "wrote Log.base" in first and "kept .vault/schema.yml (force overwrites)" in first
    assert "kept" not in forced and "wrote .vault/schema.yml" in forced
