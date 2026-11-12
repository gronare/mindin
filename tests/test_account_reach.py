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
                ("search", {"query": "press", "vault": "club"}),
                ("list_files", {"prefix": "Resources/", "vault": "private"}),
                ("read_file", {"path": "Resources/cider.md", "vault": "club"}),
            ],
        )
        main = await call_tool(ann, key, "list_files", {"prefix": "Resources/"})
    assert not mine.get("isError") and not shared.get("isError"), (text_of(mine), text_of(shared))
    lines = text_of(found).splitlines()
    assert lines[1].startswith("Searched 1 vault: club (Orchard club)"), lines
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
    assert "search without `vault` covers the main vault and the organisation vaults" in instructions
    assert "- `private` (Private): searched only when named" in instructions
    assert "- `club` (Orchard club): searched only when named" in instructions
    assert "An organisation page is a lead" not in instructions


def checked(page: str, slug: str) -> bool:
    return f'name="vault" value="{slug}" checked>' in page


def default_chips(listing: str) -> list[str]:
    rows = listing.split('<a class="vault"')[1:]
    return [row.split('<div class="name">', 1)[1].split(" <span", 1)[0] for row in rows if ">Default</span>" in row]


@pytest.mark.anyio
async def test_the_default_vault_is_preselected_on_both_approval_pages_and_the_chosen_vault_wins(site: Hosted):
    ann = await ann_with_three_vaults(site)
    async with ann, site.browser() as machine:
        before = await ann.get("/account")
        earlier = await device_key(site, ann, vault="notes")
        page = await ann.get("/account/vaults/private")
        made = await ann.post("/account/vaults/private/default", data={"csrf": csrf_of(page.text)})
        listed = await ann.get("/account")
        shown = await ann.get("/account/vaults/private")
        client = await register(ann)
        _, started = await authorize(ann, client["client_id"])
        consent_page, approved = await consent(ann, started.headers["location"], vault="notes")
        device = await start(machine)
        device_page = await ann.get("/device", params={"user_code": device["user_code"]})
        chosen = await device_key(site, ann, vault="notes")
        landed = await call_tool(
            ann, earlier, "write_file", {"path": "Resources/beds.md", "content": note("Beds", "Raised beds.")}
        )
        in_notes = await call_tool(ann, chosen, "list_files", {"prefix": "Resources/", "vault": "notes"})
        started_session = await post_mcp(ann, chosen, INITIALIZE)
    assert default_chips(before.text) == ["Personal"], "a new account's first vault is its default"
    assert made.status_code == 303
    assert default_chips(listed.text) == ["Private"], "an account has one default vault"
    assert "This is your default vault" in shown.text and "Make default" not in shown.text
    assert checked(consent_page.text, "private") and not checked(consent_page.text, "notes")
    assert checked(device_page.text, "private") and not checked(device_page.text, "notes")
    assert approved.status_code == 302 and "code=" in approved.headers["location"]
    assert not landed.get("isError"), text_of(landed)
    assert text_of(in_notes) == "Resources/beds.md", "an earlier connection keeps its vault"
    instructions = sse_payload(started_session.text)["result"]["instructions"]
    assert "The main vault is Personal (`notes`)" in instructions, "the vault picked on the page wins over the default"


@pytest.mark.anyio
async def test_only_a_vault_the_account_owns_can_be_its_default(site: Hosted):
    ann = await ann_with_three_vaults(site)
    async with ann:
        page = await ann.get("/account/vaults/club")
        refused = await ann.post("/account/vaults/club/default", data={"csrf": csrf_of(page.text)})
        listed = await ann.get("/account")
    assert "Make default" not in page.text
    assert refused.status_code == 403
    assert default_chips(listed.text) == ["Personal"]


@pytest.mark.anyio
async def test_a_connection_searches_only_its_own_vault_and_reaches_the_others_by_name(site: Hosted):
    ann = await ann_with_three_vaults(site)
    async with ann:
        key = await device_key(site, ann, vault="private")
        for slug in ("notes", "private", "club"):
            page = note(f"Press in {slug}", "How the press works.")
            written = await call_tool(
                ann, key, "write_file", {"path": f"Resources/press-{slug}.md", "content": page, "vault": slug}
            )
            assert not written.get("isError"), text_of(written)
        found, named, shared = await in_one_session(
            ann,
            key,
            [
                ("search", {"query": "press"}),
                ("search", {"query": "press", "vault": "notes"}),
                ("search", {"query": "press", "vault": "club"}),
            ],
        )
    assert "Searched" not in text_of(found) and "Resources/press-private.md" in text_of(found), text_of(found)
    assert "press-notes" not in text_of(found) and "press-club" not in text_of(found), text_of(found)
    assert "notes | Resources/press-notes.md" in text_of(named), text_of(named)
    assert "club | Resources/press-club.md" in text_of(shared), text_of(shared)


@pytest.mark.anyio
async def test_session_start_lists_no_pages_from_shared_or_other_personal_vaults(site: Hosted):
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
    assert "organisation pages" not in text_of(bundle), text_of(bundle)
    assert "Cider press" not in text_of(bundle) and "Private press" not in text_of(bundle)
