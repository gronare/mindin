from __future__ import annotations

import json
from pathlib import Path

import pytest
from hosted_support import INITIALIZE, Hosted, call_tool, hosted, post_mcp, sse_payload, text_of
from test_shared_vaults import PRUNING, club, key_of, write

from mindin.hooks import SHARED_VAULT_HINT, VAULT_HINT, render_session_start
from mindin.schema import instructions
from mindin.server import build_server
from mindin.vault import SubjectVaults, Vault


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


async def instructions_at(site: Hosted, http, key: str, path: str) -> str:
    response = await post_mcp(http, key, INITIALIZE, path=path)
    assert response.status_code == 200, response.text
    return sse_payload(response.text)["result"]["instructions"]


@pytest.mark.anyio
async def test_a_shared_vault_tells_the_agent_what_belongs_there_and_a_personal_vault_keeps_its_text(
    site: Hosted, vaults: SubjectVaults
):
    ann, bo = await club(site)
    shared_key = key_of(site, "bo@example.com", "bo laptop")
    personal_key = key_of(site, "bo@example.com", "bo laptop", slug="notes")
    async with ann, bo:
        shared = await instructions_at(site, bo, shared_key, "/mcp?vault=orchardclub")
        personal = await instructions_at(site, bo, personal_key, "/mcp?vault=notes")
    assert shared.startswith("This is Orchard club, a shared vault.")
    assert "they go in the user's personal vault" in shared
    assert "- Areas/ — " in shared and "- Resources/ — " in shared and "Projects/" not in shared
    for workflow in ("backlog_add` it", "`log_append` one line", "repo log", "The moment something is deferred"):
        assert workflow not in shared
    assert personal == instructions(vaults.instruction_schema)
    assert "`backlog_add` it in the same" in personal


def test_a_local_vault_keeps_the_instructions_of_its_schema(vault: Vault):
    server = build_server(vault)
    assert server._lowlevel_server.create_initialization_options().instructions == instructions(vault.schema)


@pytest.mark.anyio
async def test_the_session_start_text_of_a_shared_vault_never_mentions_the_backlog(site: Hosted):
    ann, bo = await club(site)
    key = key_of(site, "bo@example.com", "bo laptop")
    personal_key = key_of(site, "bo@example.com", "bo laptop", slug="notes")
    async with ann, bo:
        await write(bo, key, "Areas/orchard.md", PRUNING.replace("title: Pruning", "title: Orchard"))
        shared = await call_tool(bo, key, "context", {"repo": "orchard", "format": "json"}, "/mcp?vault=orchardclub")
        personal = await call_tool(
            bo, personal_key, "context", {"repo": "orchard", "format": "json"}, "/mcp?vault=notes"
        )
    shared_text = render_session_start(json.loads(text_of(shared)), "/nowhere")
    personal_text = render_session_start(json.loads(text_of(personal)), "/nowhere")
    assert shared_text.endswith(SHARED_VAULT_HINT)
    assert "backlog_add" not in shared_text and "## backlog" not in shared_text and "log_append" not in shared_text
    assert "the user's personal vault" in shared_text
    assert personal_text.endswith(VAULT_HINT)
