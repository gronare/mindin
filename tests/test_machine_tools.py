from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from hosted_support import BASE, Hosted, call_tool, hosted, open_session, post_mcp, sse_payload, text_of

from notes_vault_mcp.auth import MACHINE_KEY_PURPOSE
from notes_vault_mcp.server import MACHINE_TOOLS, build_server
from notes_vault_mcp.vault import SubjectVaults, Vault

CLAIM = {"path": "Projects/x.md", "machine": "m", "task": "t"}


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        await (await running.member("ann@example.com")).aclose()
        yield running


def device_key(site: Hosted) -> str:
    account = site.store.account("ann@example.com")
    return site.store.create_key(account.id, "devbox", ["vault:read", "vault:write"], purpose=MACHINE_KEY_PURPOSE)


def connector_token(site: Hosted) -> str:
    account = site.store.account("ann@example.com")
    token = secrets.token_urlsafe(32)
    grant = {
        "grant_id": secrets.token_urlsafe(8),
        "client_id": "connector",
        "account_id": account.id,
        "scopes": ["vault:read", "vault:write"],
        "resource": f"{BASE}/mcp",
    }
    site.store.save_token(token, "access", grant, 3600)
    return token


async def listed(site: Hosted, token: str) -> set[str]:
    async with site.browser() as http:
        session = await open_session(http, token)
        response = await post_mcp(http, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
    return {tool["name"] for tool in sse_payload(response.text)["result"]["tools"]}


async def claimed(site: Hosted, token: str) -> dict:
    async with site.browser() as http:
        return await call_tool(http, token, "claim", CLAIM)


@pytest.mark.anyio
async def test_a_connector_token_neither_lists_nor_calls_the_machine_tools(site: Hosted):
    token = connector_token(site)
    assert not MACHINE_TOOLS & await listed(site, token)
    refused = await claimed(site, token)
    assert refused.get("isError") and "Unknown tool: claim" in text_of(refused)


@pytest.mark.anyio
async def test_a_key_made_on_the_account_page_neither_lists_nor_calls_them(site: Hosted):
    key = site.key_for("ann@example.com")
    assert not MACHINE_TOOLS & await listed(site, key)
    refused = await claimed(site, key)
    assert refused.get("isError") and "Unknown tool: claim" in text_of(refused)


@pytest.mark.anyio
async def test_a_device_flow_key_lists_and_calls_them(site: Hosted):
    key = device_key(site)
    assert await listed(site, key) >= MACHINE_TOOLS
    answered = await claimed(site, key)
    assert "Unknown tool" not in text_of(answered)


@pytest.mark.anyio
async def test_the_local_stdio_server_has_no_machine_tools(vault: Vault):
    from mcp.client import Client

    async with Client(build_server(vault)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        refused = await client.call_tool("claim", CLAIM)
    assert not MACHINE_TOOLS & names
    assert refused.is_error
