from __future__ import annotations

from pathlib import Path

import pytest
from mcp.client import Client

from mindin.schema import schema_from_text
from mindin.server import build_server
from mindin.vault import Vault

ORCHARD = str(Path("~/projects/orchard").expanduser())

TOOL_NAMES = {
    "search",
    "changes",
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
    "whoami",
    "rules",
    "template",
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
    async with Client(build_server(vault)) as client:
        told = client.instructions
    assert "Areas/" in told
    assert "status is one of: draft, active, backlog, complete, superseded" in told


@pytest.mark.anyio
async def test_search_returns_rendered_rows(vault: Vault):
    async with Client(build_server(vault)) as client:
        text = await text_of(client, "search", {"query": "orchard", "limit": 3})
    assert text.startswith("3 of 5 (archive: 2 hidden, superseded: 0 hidden)")


@pytest.mark.anyio
async def test_search_without_a_limit_takes_the_schemas_default_limit(vault: Vault, vault_dir: Path):
    schema = vault_dir / ".vault" / "schema.yml"
    schema.write_text(schema.read_text(encoding="utf-8") + "search:\n  default_limit: 3\n", encoding="utf-8")
    vault.reload_schema()
    async with Client(build_server(vault)) as client:
        default = await text_of(client, "search", {"query": "orchard"})
        explicit = await text_of(client, "search", {"query": "orchard", "limit": 4})
    assert default.startswith("3 of 5 ")
    assert explicit.startswith("4 of 5 ")


@pytest.mark.anyio
async def test_a_negative_limit_answers_the_count_and_no_rows_from_the_end(vault: Vault):
    async with Client(build_server(vault)) as client:
        searched = await text_of(client, "search", {"query": "orchard", "limit": -2})
        for title in ("First idea", "Second idea"):
            await text_of(client, "backlog_add", {"title": title, "area": "orchard", "line": "later"})
        listed = await text_of(client, "backlog", {"limit": -1})
        one = await text_of(client, "backlog", {"limit": 1})
        huge = await text_of(client, "backlog", {"limit": 10**12})
        bundle = await text_of(client, "context", {"path": ORCHARD, "repo": "orchard", "limit": -1})
    assert searched.startswith("0 of 5 ")
    assert "## open tasks" not in bundle and "## system notes" in bundle
    assert listed == "2 backlog items, the first 0 shown"
    assert one.splitlines()[0] == "2 backlog items, the first 1 shown" and len(one.splitlines()) == 2
    assert huge.splitlines()[0] == "2 backlog items"


@pytest.mark.parametrize(
    ("line", "notice", "limit", "title_weight"),
    [
        ("default_limit: -1", "search.default_limit is not a whole number of 1 or more, so search uses 15", 15, 10.0),
        ("default_limit: 2.5", "search.default_limit is not a whole number of 1 or more, so search uses 15", 15, 10.0),
        ("recency_half_life_days: 0", "search.recency_half_life_days is not above 0, so search uses 90", 15, 10.0),
        ("title_weight: .inf", "search.title_weight is not a finite number, so search uses 10", 15, 10.0),
        (f"title_weight: {10**400}", "search.title_weight is not a finite number, so search uses 10", 15, 10.0),
        (f"default_limit: {10**400}", "search.default_limit is not a finite number, so search uses 15", 15, 10.0),
        ("title_weight: -1", "search.title_weight is below 0, so search uses 10", 15, 10.0),
    ],
)
def test_a_search_setting_that_would_break_search_falls_back_and_keeps_the_layout(
    line: str, notice: str, limit: int, title_weight: float
):
    schema = schema_from_text(f"preset: para\nsearch:\n  {line}\n")
    assert schema.problems == ()
    assert any(found.startswith(notice) for found in schema.notices), schema.notices
    assert schema.preset == "para"
    assert schema.default_limit == limit and schema.bm25_weights[0] == title_weight
    assert schema.recency_half_life_days > 0


def test_a_default_limit_above_the_cap_answers_the_cap():
    schema = schema_from_text("search:\n  default_limit: 100000000\n")
    assert schema.problems == () and schema.notices == () and schema.default_limit == 1000


def test_a_whole_number_written_as_a_float_is_a_valid_default_limit():
    schema = schema_from_text("search:\n  default_limit: 20.0\n")
    assert schema.notices == () and schema.default_limit == 20


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
        forced = await client.call_tool("init", {"force": True})
    assert "wrote Log.base" in first and "kept .vault/schema.yml" in first
    assert forced.is_error and "already has a layout" in forced.content[0].text


@pytest.mark.anyio
async def test_the_server_introduces_itself_as_mindin(vault: Vault):
    async with Client(build_server(vault)) as client:
        assert client.server_info.name == "mindin"


def test_a_bad_search_setting_falls_back_to_the_layouts_own_value():
    schema = schema_from_text("preset: wiki\nsearch:\n  recency_half_life_days: 0\n")
    assert schema.problems == () and schema.recency_half_life_days == 180
    assert schema.notices == ("search.recency_half_life_days is not above 0, so search uses 180 instead",)
