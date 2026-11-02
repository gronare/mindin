from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx2
import pytest
from hosted_support import NOTE_BODY, Hosted, call_tool, csrf_of, hosted, post_mcp, text_of

from notes_vault_mcp.auth.hosted_keys import VaultKeys
from notes_vault_mcp.auth.hosted_web import RECENT_SIGN_IN
from notes_vault_mcp.vault import SubjectVaults


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


async def member_with_a_note(site: Hosted) -> tuple[httpx2.AsyncClient, str]:
    http = await site.member("ann@example.com", sub="sub-ann")
    key = site.key_for("ann@example.com")
    written = await call_tool(http, key, "write_file", {"path": "Areas/secret.md", "content": NOTE_BODY})
    assert not written.get("isError"), text_of(written)
    return http, key


async def confirm(http: httpx2.AsyncClient, email: str) -> httpx2.Response:
    form = await http.get("/account/delete")
    return await http.post("/account/delete", data={"csrf": csrf_of(form.text), "email": email})


@pytest.mark.anyio
async def test_the_account_page_links_to_deletion_and_the_confirmation_says_what_goes(site: Hosted):
    http, _ = await member_with_a_note(site)
    async with http:
        account = (await http.get("/account/settings")).text
        page = await http.get("/account/delete")
    assert 'href="/account/delete"' in account
    assert page.status_code == 200
    assert "Type ann@example.com to confirm" in page.text
    assert "files, and the key they are encrypted with" in page.text
    assert "1 keys, every way you sign in, every session and every connected client" in page.text
    assert 'href="/account"' in page.text


@pytest.mark.anyio
async def test_typing_the_address_deletes_the_vault_the_key_the_sessions_and_the_tokens(
    site: Hosted, vaults: SubjectVaults, storage: Path, tmp_path: Path
):
    http, key = await member_with_a_note(site)
    account = site.store.account("ann@example.com")
    vault_id = site.vault_of("ann@example.com")
    assert (storage / "users" / vault_id).is_dir()
    async with http:
        deleted = await confirm(http, " Ann@Example.com ")
        after = await http.get("/account")
        refused = await post_mcp(http, key, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert deleted.status_code == 200, deleted.text
    assert "ann@example.com and its vaults are gone" in deleted.text
    assert after.status_code == 303
    assert refused.status_code == 401
    assert site.store.account(account.id) is None
    assert not VaultKeys(site.store, site.keyring).has_key(vault_id)
    assert not (storage / "users" / vault_id).exists()
    assert not list((tmp_path / "cache").glob("*.sqlite*"))
    assert vaults.open_subjects == []
    with sqlite3.connect(site.store.database.path) as connection:
        for table in ("sessions", "tokens", "api_keys", "identities", "vaults"):
            count = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id = ?", (account.id,))
            assert count.fetchone()[0] == 0, table


@pytest.mark.anyio
async def test_a_wrong_address_deletes_nothing(site: Hosted, storage: Path):
    http, _ = await member_with_a_note(site)
    account = site.store.account("ann@example.com")
    async with http:
        refused = await confirm(http, "bo@example.com")
    assert refused.status_code == 400
    assert "not this account" in refused.text
    assert site.store.account(account.id) is not None
    assert (storage / "users" / site.vault_of("ann@example.com")).is_dir()


@pytest.mark.anyio
async def test_deletion_needs_a_sign_in_from_the_last_ten_minutes(site: Hosted, storage: Path):
    http, _ = await member_with_a_note(site)
    account = site.store.account("ann@example.com")
    async with http:
        form = await http.get("/account/delete")
        site.clock.advance(RECENT_SIGN_IN + 1)
        stale = await http.get("/account/delete")
        refused = await http.post("/account/delete", data={"csrf": csrf_of(form.text), "email": account.email})
        again = await http.post("/account/delete/again", data={"csrf": csrf_of(stale.text)})
        signed_out = await http.get("/account")
    assert "Sign in again" in stale.text and "Type ann@example.com" not in stale.text
    assert refused.status_code == 403
    assert site.store.account(account.id) is not None
    assert (storage / "users" / site.vault_of("ann@example.com")).is_dir()
    assert again.status_code == 303
    assert again.headers["location"] == "/signin?next=%2Faccount%2Fdelete"
    assert signed_out.status_code == 303


@pytest.mark.anyio
async def test_deletion_needs_the_pages_csrf_token(site: Hosted):
    http, _ = await member_with_a_note(site)
    async with http:
        refused = await http.post("/account/delete", data={"csrf": "wrong", "email": "ann@example.com"})
    assert refused.status_code == 403
    assert site.store.account("ann@example.com") is not None
