from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from hosted_support import BASE, INITIALIZE, Hosted, call_tool, csrf_of, hosted, post_mcp, sse_payload, text_of
from test_hosted_app import authorize, consent, register
from test_organisations import in_one_session
from test_signin_device import decide, poll, start

from mindin.hosted.accounts.reach import ALSO_REACHES
from mindin.hosted.pages.html import esc
from mindin.vault import SubjectVaults

WRITE = ["vault:read", "vault:write"]


def note(title: str, body: str) -> str:
    return (
        f"---\ntitle: {title}\ndate: 2026-11-01\nupdated: 2026-11-01\nstatus: active\ntags: [orchard]\n"
        f'kind: howto\narea: "[[orchard]]"\n---\n\n{body}\n'
    )


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


async def ann_with_three_vaults(site: Hosted) -> httpx2.AsyncClient:
    bo = await site.member("bo@example.com")
    await bo.aclose()
    bo_id = site.store.account("bo@example.com").id
    club = site.store.create_vault(bo_id, "Orchard club", "club", kind="shared")
    site.store.invite_member(club, "ann@example.com", bo_id)
    ann = await site.member("ann@example.com")
    site.store.create_vault(site.store.account("ann@example.com").id, "Private", "private")
    return ann


async def device_key(site: Hosted, ann: httpx2.AsyncClient, access: str = "write", vault: str = "notes") -> str:
    async with site.browser() as machine:
        device = await start(machine)
        page, approved = await decide(ann, device["user_code"], access=access, vault=vault)
        assert approved.status_code == 200, approved.text
        granted = await poll(site, machine, device)
    assert granted.status_code == 200, granted.text
    return granted.json()["access_token"]


@pytest.mark.anyio
async def test_a_signed_in_machine_reaches_the_accounts_second_vault_and_a_shared_vault_by_name(site: Hosted):
    ann = await ann_with_three_vaults(site)
    async with ann:
        key = await device_key(site, ann)
        mine, shared, found, listed, read = await in_one_session(
            ann,
            key,
            [
                (
                    "write_file",
                    {"path": "Resources/pruning.md", "content": note("Pruning", "Prune in March."), "vault": "private"},
                ),
                (
                    "write_file",
                    {"path": "Resources/cider.md", "content": note("Cider", "Press in October."), "vault": "club"},
                ),
                ("search", {"query": "press"}),
                ("list_files", {"prefix": "Resources/", "vault": "private"}),
                ("read_file", {"path": "Resources/cider.md", "vault": "club"}),
            ],
        )
        main = await call_tool(ann, key, "list_files", {"prefix": "Resources/"})
    assert not mine.get("isError") and not shared.get("isError"), (text_of(mine), text_of(shared))
    lines = text_of(found).splitlines()
    assert lines[1].startswith("Searched 3 vaults: notes (Personal), private (Private), club (Orchard club)"), lines
    assert lines[2].startswith("club | Resources/cider.md | Cider"), lines
    assert text_of(listed) == "Resources/pruning.md"
    assert "Press in October." in text_of(read)
    assert "pruning" not in text_of(main), (
        "a write without vault goes to the main vault, and only a named one leaves it"
    )


@pytest.mark.anyio
async def test_an_api_key_reaches_only_its_own_vault(site: Hosted):
    ann = await ann_with_three_vaults(site)
    async with ann:
        key = site.store.create_key(site.store.account("ann@example.com").id, "laptop", WRITE)
        other = await call_tool(ann, key, "list_files", {"vault": "private"})
        found = await call_tool(ann, key, "search", {"query": "welcome"})
    assert other.get("isError") and "does not reach a vault 'private'" in text_of(other)
    assert "Searched" not in text_of(found)


@pytest.mark.anyio
async def test_a_read_only_connection_refuses_a_write_to_every_vault_it_reaches(site: Hosted):
    ann = await ann_with_three_vaults(site)
    async with ann:
        key = await device_key(site, ann, access="read")
        refused = await call_tool(
            ann, key, "write_file", {"path": "Resources/x.md", "content": note("X", "X."), "vault": "club"}
        )
        read = await call_tool(ann, key, "list_files", {"vault": "club"})
    assert refused.get("isError") and "write" in text_of(refused).lower()
    assert not read.get("isError"), text_of(read)


@pytest.mark.anyio
async def test_a_connection_made_before_account_reach_keeps_its_former_reach_until_it_is_made_again(site: Hosted):
    ann = await ann_with_three_vaults(site)
    async with ann:
        account = site.store.account("ann@example.com").id
        old = site.store.create_key(account, "devbox", WRITE, purpose="device")
        refused = await call_tool(ann, old, "list_files", {"vault": "private"})
        found = await call_tool(ann, old, "search", {"query": "welcome"})
    assert refused.get("isError") and "does not reach a vault 'private'" in text_of(refused)
    assert "Searched" not in text_of(found)


@pytest.mark.anyio
async def test_a_slug_the_account_owns_wins_over_a_shared_vault_at_the_same_address(site: Hosted):
    ann = await ann_with_three_vaults(site)
    async with ann:
        bo_id = site.store.account("bo@example.com").id
        theirs = site.store.create_vault(bo_id, "Bo's garden", "garden", kind="shared")
        ann_id = site.store.account("ann@example.com").id
        site.store.create_vault(ann_id, "Garden", "garden")
        with site.store.connect() as connection:
            connection.execute(
                "INSERT INTO vault_members(vault_id, account_id, joined_at) VALUES(?, ?, 1)", (theirs.id, ann_id)
            )
        key = await device_key(site, ann)
        written = await call_tool(
            ann,
            key,
            "write_file",
            {"path": "Resources/beds.md", "content": note("Beds", "Raised beds."), "vault": "garden"},
        )
        own = site.store.vault_at(ann_id, "garden")
        assert own is not None and own.account_id == ann_id
        landed = await call_tool(
            ann, site.store.create_key(ann_id, "garden", WRITE, vault_id=own.id), "list_files", {"prefix": "Resources/"}
        )
    assert not written.get("isError"), text_of(written)
    assert text_of(landed) == "Resources/beds.md", "the account's own vault answers the bare slug"


@pytest.mark.anyio
async def test_the_consent_page_and_the_device_page_say_the_connection_reaches_every_vault(site: Hosted):
    ann = await ann_with_three_vaults(site)
    async with ann, site.browser() as machine:
        client = await register(ann)
        _, started = await authorize(ann, client["client_id"])
        page, _ = await consent(ann, started.headers["location"], decision="deny")
        device = await start(machine)
        confirm = await ann.get("/device", params={"user_code": device["user_code"]})
    for shown in (page.text, confirm.text):
        assert "Main vault" in shown and esc(ALSO_REACHES) in shown, shown
    assert "is asking to reach your vaults." in page.text


@pytest.mark.anyio
async def test_a_claude_ai_connection_reaches_every_vault_and_keeps_it_through_a_refresh(site: Hosted):
    ann = await ann_with_three_vaults(site)
    async with ann:
        client = await register(ann)
        verifier, started = await authorize(ann, client["client_id"])
        _, approved = await consent(ann, started.headers["location"], vault="notes")
        code = httpx2.URL(approved.headers["location"]).params["code"]
        tokens = (
            await ann.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "http://localhost:9876/callback",
                    "client_id": client["client_id"],
                    "code_verifier": verifier,
                    "resource": f"{BASE}/mcp",
                },
            )
        ).json()
        refreshed = await ann.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"],
            },
        )
        reached = await call_tool(ann, refreshed.json()["access_token"], "list_files", {"vault": "club"})
        started_session = await post_mcp(ann, tokens["access_token"], INITIALIZE)
    assert not reached.get("isError"), text_of(reached)
    instructions = sse_payload(started_session.text)["result"]["instructions"]
    assert "## Vaults this connection reaches" in instructions
    assert "The main vault is Personal (`notes`)" in instructions
    assert "- `private` (Private): searched by default" in instructions
    assert "- `club` (Orchard club): searched by default" in instructions


@pytest.mark.anyio
async def test_search_skips_a_vault_marked_off_and_searches_it_when_named(site: Hosted):
    ann = await ann_with_three_vaults(site)
    async with ann:
        key = await device_key(site, ann)
        pruning = {"path": "Resources/pruning.md", "content": note("Pruning", "Prune in March."), "vault": "private"}
        assert not (await call_tool(ann, key, "write_file", pruning)).get("isError")
        page = await ann.get("/account/vaults/private")
        turned = await ann.post("/account/vaults/private/search", data={"csrf": csrf_of(page.text)})
        listed = await ann.get("/account")
        everywhere, named, written = await in_one_session(
            ann,
            key,
            [
                ("search", {"query": "pruning"}),
                ("search", {"query": "pruning", "vault": "private"}),
                ("append_file", {"path": "Resources/pruning.md", "content": "Also in June.", "vault": "private"}),
            ],
        )
        started_session = await post_mcp(ann, key, INITIALIZE)
        after = await ann.get("/account/vaults/private")
    assert 'name="searched" value="on" checked' in page.text
    assert "Search this vault by default from my other connections" in page.text
    assert turned.status_code == 303
    assert 'name="searched" value="on">' in after.text, "the box shows the preference unchecked once it is off"
    assert "not searched by default" in listed.text.split("Private", 1)[1].split("</a>", 1)[0]
    assert "No notes matched." in text_of(everywhere) and "private (Private)" not in text_of(everywhere)
    lines = text_of(named).splitlines()
    assert lines[1].startswith("Searched 1 vault: private (Private)") and lines[2].startswith("private | "), lines
    assert not written.get("isError"), text_of(written)
    instructions = sse_payload(started_session.text)["result"]["instructions"]
    assert "- `private` (Private): not searched by default" in instructions


@pytest.mark.anyio
async def test_a_vault_marked_off_is_still_searched_from_a_connection_made_for_it(site: Hosted):
    ann = await ann_with_three_vaults(site)
    async with ann:
        site.store.set_searched_by_default(
            site.store.account("ann@example.com").id, site.vault_of("ann@example.com", "private"), False
        )
        key = await device_key(site, ann, vault="private")
        found = await call_tool(ann, key, "search", {"query": "welcome"})
    assert "Searched 3 vaults: private (Private)" in text_of(found)


@pytest.mark.anyio
async def test_session_start_lists_pages_about_the_repo_from_shared_vaults_and_not_from_other_personal_vaults(
    site: Hosted,
):
    ann = await ann_with_three_vaults(site)
    async with ann:
        key = await device_key(site, ann)
        for slug, title in (("club", "Cider press"), ("private", "Private press")):
            page = note(title, "How the press works.").replace("tags: [orchard]", "tags: [orchard]\nrepos: [press]")
            written = await call_tool(
                ann, key, "write_file", {"path": "Resources/press.md", "content": page, "vault": slug}
            )
            assert not written.get("isError"), text_of(written)
        bundle = await call_tool(ann, key, "context", {"path": "/src/press", "repo": "press"})
        site.store.set_searched_by_default(
            site.store.account("ann@example.com").id,
            site.store.vault_at(site.store.account("ann@example.com").id, "club").id,
            False,
        )
        skipped = await call_tool(ann, key, "context", {"path": "/src/press", "repo": "press"})
    assert "club | Resources/press.md | Cider press" in text_of(bundle), text_of(bundle)
    assert "Private press" not in text_of(bundle)
    assert "Cider press" not in text_of(skipped)
