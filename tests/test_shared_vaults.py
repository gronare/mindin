from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import httpx2
import pytest
from hosted_support import BASE, INITIALIZE, Hosted, call_tool, csrf_of, hosted, open_session, post_mcp, query, text_of
from login_support import branded, hrefs

from mindin.auth import MACHINE_KEY_PURPOSE
from mindin.auth.database import SqliteDatabase
from mindin.auth.hosted_access import SupportAccess
from mindin.auth.hosted_keys import Keyring, MasterKey, SealedVaults, VaultKeys
from mindin.auth.hosted_store import HostedStore, MembershipError, VaultNameError
from mindin.auth.hosted_vaults import MIGRATIONS, Vault
from mindin.backends import SCHEMA_KEY
from mindin.cli import main
from mindin.vault import SubjectVaults

WRITE = ["vault:read", "vault:write"]
FRONT = "date: 2026-09-01\nupdated: 2026-09-01\nstatus: active\n"
PRUNING = f"---\ntitle: Pruning\n{FRONT}tags: [orchard]\nkind: system\n---\n\nPrune the apple trees in March.\n"
PRUNING_LATER = PRUNING.replace("in March", "in late February")
LATHE = (
    f'---\ntitle: Lathe oil\n{FRONT}tags: [workshop]\nkind: howto\narea: "[[pruning]]"\n---\n\nOil the bed weekly.\n'
)
CHORES = f"---\ntitle: Chores\n{FRONT}tags: [orchard]\nkind: task\n---\n\nMulch the beds.\n"


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


def basic(secret: str, user: str = "bo@example.com") -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()}


async def make_shared(site: Hosted, http: httpx2.AsyncClient, name: str = "Orchard club") -> httpx2.Response:
    form = await http.get("/account/vaults/new")
    return await http.post("/account/vaults", data={"csrf": csrf_of(form.text), "name": name, "kind": "shared"})


async def club(site: Hosted) -> tuple[httpx2.AsyncClient, httpx2.AsyncClient]:
    ann = await site.member("ann@example.com", sub="sub-ann")
    site.store.rename_vault(site.vault_of("ann@example.com"), "Ann's orchard", "annorchard")
    made = await make_shared(site, ann)
    assert made.status_code == 303 and made.headers["location"] == "/account/vaults/orchardclub", made.text
    vault = club_vault(site)
    site.store.invite_member(vault, "bo@example.com", site.store.account("ann@example.com").id)
    bo = await site.member("bo@example.com", sub="sub-bo")
    return ann, bo


def club_vault(site: Hosted, slug: str = "orchardclub") -> Vault | None:
    owned = (vault for account in site.store.accounts() for vault in site.store.vaults(account.id))
    return next((vault for vault in owned if vault.slug == slug), None)


def key_of(site: Hosted, email: str, name: str, slug: str = "orchardclub", purpose: str = "api") -> str:
    account = site.store.account(email)
    vault = site.store.vault_at(account.id, slug) or club_vault(site, slug)
    return site.store.create_key(account.id, name, WRITE, purpose=purpose, vault_id=vault.id)


def changes(site: Hosted, slug: str = "orchardclub") -> list:
    vault = club_vault(site, slug)
    return SealedVaults(site.store, VaultKeys(site.store, site.keyring)).history(vault.id).changes()


async def write(http: httpx2.AsyncClient, key: str, path: str, content: str) -> dict:
    return await call_tool(http, key, "write_file", {"path": path, "content": content}, path="/mcp?vault=orchardclub")


# --- membership -----------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_member_sees_the_shared_vault_and_never_the_owners_personal_vaults(site: Hosted):
    ann, bo = await club(site)
    async with ann, bo:
        listed = (await bo.get("/account")).text
        own = (await ann.get("/account")).text
        personal = await bo.get("/account/vaults/annorchard")
        shared = await bo.get("/account/vaults/orchardclub")
    assert "Orchard club" in listed and '<span class="chip role">Member</span>' in listed
    assert "Ann&#x27;s orchard" not in listed and "annorchard" not in listed
    assert '<span class="chip role">Owner</span>' in own and "Ann&#x27;s orchard" in own
    assert personal.status_code == 404
    assert shared.status_code == 200 and "Leave this vault" in shared.text
    assert "Send invitation" not in shared.text and "Hand over or delete" not in shared.text
    with pytest.raises(ValueError):
        key_of(site, "bo@example.com", "peek", slug="annorchard")


@pytest.mark.anyio
async def test_a_removed_members_key_and_token_are_refused_at_once_even_on_an_open_connection(site: Hosted):
    ann, bo = await club(site)
    key = key_of(site, "bo@example.com", "bo laptop")
    vault = club_vault(site)
    member = site.store.account("bo@example.com")
    grant = {
        "grant_id": "g-bo",
        "client_id": "c-bo",
        "account_id": member.id,
        "vault_id": vault.id,
        "scopes": WRITE,
        "resource": f"{BASE}/mcp?vault=orchardclub",
    }
    site.store.save_token("token-of-bo", "access", grant, 3600)
    ping = {"jsonrpc": "2.0", "id": 7, "method": "ping"}
    async with ann, bo:
        session = await open_session(bo, key, "/mcp?vault=orchardclub")
        before = await post_mcp(bo, key, ping, session, "/mcp?vault=orchardclub")
        by_token = await post_mcp(bo, "token-of-bo", INITIALIZE, path="/mcp?vault=orchardclub")
        dav_before = await bo.get("/dav/vault/", headers=basic(key))
        page = await ann.get("/account/vaults/orchardclub")
        removed = await ann.post(
            "/account/vaults/orchardclub/members/remove", data={"csrf": csrf_of(page.text), "account": member.id}
        )
        after = await post_mcp(bo, key, ping, session, "/mcp?vault=orchardclub")
        token_after = await post_mcp(bo, "token-of-bo", INITIALIZE, path="/mcp?vault=orchardclub")
        dav_after = await bo.request("PROPFIND", "/dav/vault/", headers=basic(key))
        page_after = await bo.get("/account/vaults/orchardclub")
    assert before.status_code == 200 and by_token.status_code == 200 and dav_before.status_code != 401
    assert removed.status_code == 200 and "no longer work" in removed.text
    assert after.status_code == 401 and token_after.status_code == 401 and dav_after.status_code == 401
    assert page_after.status_code == 404
    with sqlite3.connect(site.store.database.path) as connection:
        live = connection.execute(
            "SELECT COUNT(*) FROM api_keys WHERE account_id = ? AND vault_id = ? AND revoked_at IS NULL",
            (member.id, vault.id),
        ).fetchone()[0]
    assert live == 0


@pytest.mark.anyio
async def test_an_invitation_opens_an_account_under_invite_registration_with_one_email(site: Hosted):
    assert site.config.registration == "invite"
    ann = await site.member("ann@example.com", sub="sub-ann")
    async with ann, site.browser() as cy:
        await make_shared(site, ann)
        page = await ann.get("/account/vaults/orchardclub")
        sent = await ann.post(
            "/account/vaults/orchardclub/members/invite", data={"csrf": csrf_of(page.text), "email": "Cy@Example.com"}
        )
        listed = await ann.get("/account/vaults/orchardclub")
        assert site.store.account("cy@example.com") is None
        token = query(site.smtp.last_link("cy@example.com"))["token"]
        shown = await cy.get("/signin/email/confirm", params={"token": token})
        signed_in = await cy.post("/signin/email/confirm", data={"token": token})
        landing = await cy.get(signed_in.headers["location"])
        vaults = await cy.get("/account")
    assert sent.status_code == 200 and "An invitation is on its way to cy@example.com" in sent.text
    assert "cy@example.com" in listed.text and "Invited" in listed.text and "Withdraw" in listed.text
    assert (
        len(site.smtp.messages) == 1
        and site.smtp.messages[0]["Subject"] == "ann@example.com invites you to Orchard club"
    )
    invitation = branded(site.smtp.messages[0])
    assert "Join the vault</a>" in invitation.html and len(hrefs(invitation.html)) == 2
    assert shown.status_code == 200
    assert signed_in.status_code == 303 and signed_in.headers["location"] == "/account/vaults/orchardclub"
    assert landing.status_code == 200 and "Orchard club" in landing.text
    created = site.store.account("cy@example.com")
    assert created is not None and created.active
    assert site.store.role(club_vault(site), created.id) == "member"
    assert '<span class="chip role">Member</span>' in vaults.text


@pytest.mark.anyio
async def test_a_withdrawn_invitation_lets_nobody_in(site: Hosted):
    ann = await site.member("ann@example.com", sub="sub-ann")
    async with ann, site.browser() as cy:
        await make_shared(site, ann)
        page = await ann.get("/account/vaults/orchardclub")
        await ann.post(
            "/account/vaults/orchardclub/members/invite", data={"csrf": csrf_of(page.text), "email": "cy@example.com"}
        )
        token = query(site.smtp.last_link("cy@example.com"))["token"]
        page = await ann.get("/account/vaults/orchardclub")
        withdrawn = await ann.post(
            "/account/vaults/orchardclub/members/withdraw",
            data={"csrf": csrf_of(page.text), "email": "cy@example.com"},
        )
        late = await cy.post("/signin/email/confirm", data={"token": token})
    assert "is withdrawn" in withdrawn.text
    assert late.status_code == 400
    assert site.store.account("cy@example.com") is None


@pytest.mark.anyio
async def test_the_owner_hands_over_and_stays_a_member(site: Hosted):
    ann, bo = await club(site)
    key = key_of(site, "ann@example.com", "ann laptop")
    member = site.store.account("bo@example.com")
    async with ann, bo:
        page = await ann.get("/account/vaults/orchardclub")
        handed = await ann.post(
            "/account/vaults/orchardclub/hand-over", data={"csrf": csrf_of(page.text), "account": member.id}
        )
        still = await call_tool(ann, key, "read_file", {"path": SCHEMA_KEY}, path="/mcp?vault=orchardclub")
        bo_page = await bo.get("/account/vaults/orchardclub")
    vault = club_vault(site)
    assert "bo@example.com owns Orchard club now" in handed.text and "Leave this vault" in handed.text
    assert vault.account_id == member.id
    assert site.store.role(vault, site.store.account("ann@example.com").id) == "member"
    assert not still.get("isError")
    assert "Hand over or delete" in bo_page.text


@pytest.mark.anyio
async def test_a_member_leaves_and_their_key_stops(site: Hosted):
    ann, bo = await club(site)
    key = key_of(site, "bo@example.com", "bo laptop")
    async with ann, bo:
        page = await bo.get("/account/vaults/orchardclub")
        left = await bo.post("/account/vaults/orchardclub/leave", data={"csrf": csrf_of(page.text)})
        refused = await post_mcp(bo, key, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, path="/mcp?vault=orchardclub")
    assert "You left Orchard club" in left.text and 'href="/account/vaults/orchardclub"' not in left.text
    assert refused.status_code == 401


@pytest.mark.anyio
async def test_the_owner_cannot_delete_the_account_while_other_members_remain(site: Hosted, monkeypatch):
    ann, bo = await club(site)
    async with ann, bo:
        shown = await ann.get("/account/delete")
        form = await ann.get("/account/settings")
        refused = await ann.post("/account/delete", data={"csrf": csrf_of(form.text), "email": "ann@example.com"})
        monkeypatch.setenv("VAULT_HOSTED_MASTER_KEY", site.keyring.current.encoded())
        monkeypatch.setenv("VAULT_AUTH_DIR", str(site.config.auth_dir))
        cli = main(["accounts", "delete", "ann@example.com", "--yes"])
        page = await ann.get("/account/vaults/orchardclub")
        member = site.store.account("bo@example.com")
        await ann.post(
            "/account/vaults/orchardclub/members/remove", data={"csrf": csrf_of(page.text), "account": member.id}
        )
        form = await ann.get("/account/delete")
        deleted = await ann.post("/account/delete", data={"csrf": csrf_of(form.text), "email": "ann@example.com"})
    assert shown.status_code == 409 and "Orchard club" in shown.text and "Hand each of them over" in shown.text
    assert refused.status_code == 409
    assert cli == 1
    assert deleted.status_code == 200 and site.store.account("ann@example.com") is None
    assert club_vault(site) is None


@pytest.mark.anyio
async def test_a_deleted_members_changes_stay_in_the_history_under_their_email(site: Hosted):
    ann, bo = await club(site)
    key = key_of(site, "bo@example.com", "bo laptop")
    async with ann, bo:
        await write(bo, key, "Areas/pruning.md", PRUNING)
        form = await bo.get("/account/delete")
        deleted = await bo.post("/account/delete", data={"csrf": csrf_of(form.text), "email": "bo@example.com"})
        page = await ann.get("/account/vaults/orchardclub")
    assert deleted.status_code == 200
    assert [(change.action, change.author_email) for change in changes(site)] == [("created", "bo@example.com")]
    assert "created by bo@example.com · bo laptop" in page.text


@pytest.mark.anyio
async def test_a_member_connects_a_client_to_the_shared_vault_and_is_never_offered_the_owners(site: Hosted):
    from test_auth_hosted import REDIRECT, authorize, consent, register

    ann, bo = await club(site)
    async with ann, bo:
        client = await register(bo)
        verifier, started = await authorize(bo, client["client_id"])
        page, approved = await consent(bo, started.headers["location"], vault="orchardclub")
        code = query(approved.headers["location"])["code"]
        issued = await bo.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": client["client_id"],
                "code_verifier": verifier,
                "resource": f"{BASE}/mcp",
            },
        )
        token = issued.json()["access_token"]
        written = await call_tool(bo, token, "write_file", {"path": "Areas/pruning.md", "content": PRUNING})
        bo_page = await bo.get("/account/vaults/orchardclub")
        ann_page = await ann.get("/account/vaults/orchardclub")
    assert 'value="orchardclub"' in page.text and 'value="notes"' in page.text and "annorchard" not in page.text
    assert not written.get("isError"), text_of(written)
    assert changes(site)[0].client == "Cursor" and changes(site)[0].author_email == "bo@example.com"
    assert "<td>Cursor</td><td>Connector</td>" in bo_page.text
    assert "<td>Cursor</td><td>Connector</td>" not in ann_page.text


@pytest.mark.anyio
async def test_obsidian_access_in_a_shared_vault_mints_the_members_own_webdav_key(site: Hosted):
    ann, bo = await club(site)
    ann_dav = site.store.create_key(
        site.store.account("ann@example.com").id,
        "Obsidian (WebDAV)",
        WRITE,
        purpose="webdav",
        vault_id=club_vault(site).id,
    )
    key = key_of(site, "bo@example.com", "bo laptop")
    async with ann, bo:
        minted = await call_tool(bo, key, "obsidian_access", {}, "/mcp?vault=orchardclub")
        password = text_of(minted).split("Password: ")[1].split()[0]
        put = await bo.put("/dav/vault/Areas/pruning.md", content=PRUNING.encode(), headers=basic(password))
        still = await ann.request("PROPFIND", "/dav/vault/", headers=basic(ann_dav, "ann@example.com"))
    assert put.status_code == 201
    assert still.status_code == 207, "a member's new WebDAV key leaves the owner's in place"
    assert changes(site)[0].client == "Obsidian (WebDAV)" and changes(site)[0].author_email == "bo@example.com"


@pytest.mark.anyio
async def test_support_access_to_a_shared_vault_is_the_owners_to_grant(site: Hosted):
    ann, bo = await club(site)
    async with ann, bo:
        owner = (await ann.get("/account/settings")).text
        member = (await bo.get("/account/settings")).text
        form = await bo.get("/account/settings")
        tried = await bo.post(
            "/account/support", data={"csrf": csrf_of(form.text), "vault": "orchardclub", "duration": "day"}
        )
    assert '<option value="orchardclub">Orchard club</option>' in owner
    assert "orchardclub" not in member
    assert tried.status_code == 303
    assert SupportAccess(site.store).grant_until(club_vault(site).id) is None


# --- what belongs in a shared vault ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_task_and_log_notes_and_the_task_tools_are_refused_with_a_message_an_agent_can_act_on(site: Hosted):
    ann, bo = await club(site)
    key = key_of(site, "bo@example.com", "bo laptop")
    machine = key_of(site, "bo@example.com", "bo-mac", purpose=MACHINE_KEY_PURPOSE)
    path = "/mcp?vault=orchardclub"
    async with ann, bo:
        project = await write(bo, key, "Projects/chores.md", CHORES.replace("kind: task\n", ""))
        task = await write(bo, key, "Areas/chores.md", CHORES)
        filed = await call_tool(bo, key, "backlog_add", {"title": "Mulch", "area": "pruning", "line": "later"}, path)
        logged = await call_tool(bo, key, "log_append", {"repo": "orchard", "line": "pruned"}, path)
        parked = await call_tool(bo, key, "set_status", {"path": "Areas/x.md", "status": "backlog"}, path)
        listed = await call_tool(bo, key, "backlog", {}, path)
        claimed = await call_tool(bo, machine, "claim", {"path": "Areas/x.md", "machine": "m", "task": "t"}, path)
        dav_project = await bo.put("/dav/vault/Projects/chores.md", content=CHORES.encode(), headers=basic(key))
        dav_schema = await bo.put("/dav/vault/.vault/schema.yml", content=b"version: 1\n", headers=basic(key))
        fine = await write(bo, key, "Resources/lathe-oil.md", LATHE)
    for result in (project, task, filed, logged, parked, listed, claimed):
        assert result.get("isError"), text_of(result)
        assert "shared vault Orchard club" in text_of(result)
        assert "personal vault" in text_of(result)
    assert "Projects/chores.md is refused" in text_of(
        project
    ) and "notes go in Areas/, Resources/ or Archive/" in text_of(project)
    assert "Areas/ takes system notes, not task" in text_of(task)
    assert "backlog_add is refused in the shared vault Orchard club" in text_of(filed)
    assert dav_project.status_code == 403 and "Projects/chores.md is refused" in dav_project.text
    assert dav_schema.status_code == 403 and "layout of the shared vault Orchard club is fixed" in dav_schema.text
    assert not fine.get("isError"), text_of(fine)


@pytest.mark.anyio
async def test_the_session_context_of_a_shared_vault_has_no_task_backlog_or_log_sections(site: Hosted):
    ann, bo = await club(site)
    key = key_of(site, "bo@example.com", "bo laptop")
    async with ann, bo:
        await write(bo, key, "Areas/orchard.md", PRUNING.replace("title: Pruning", "title: Orchard"))
        answer = await call_tool(bo, key, "context", {"repo": "orchard", "format": "json"}, "/mcp?vault=orchardclub")
        shown = await call_tool(bo, key, "whoami", {}, "/mcp?vault=orchardclub")
    text = json.loads(text_of(answer))["text"]
    assert text.startswith("## shared vault\nOrchard club is a shared vault")
    assert "## system notes\n### Areas/orchard.md" in text
    for section in ("## open tasks", "## triage", "## backlog", "## log"):
        assert section not in text
    assert json.loads(text_of(shown)) == {
        "email": "bo@example.com",
        "vault": "orchardclub",
        "vault_name": "Orchard club",
        "vault_kind": "shared",
        "write": True,
        "role": "member",
    }


# --- history --------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_every_write_move_and_delete_through_mcp_is_in_the_history_with_account_and_client(site: Hosted):
    ann, bo = await club(site)
    key = key_of(site, "bo@example.com", "bo laptop")
    path = "/mcp?vault=orchardclub"
    async with ann, bo:
        await write(bo, key, "Areas/pruning.md", PRUNING)
        await call_tool(bo, key, "append_file", {"path": "Areas/pruning.md", "content": "Wear gloves."}, path)
        await call_tool(bo, key, "move_file", {"source": "Areas/pruning.md", "dest": "Areas/apples.md"}, path)
        await call_tool(bo, key, "delete_file", {"path": "Areas/apples.md"}, path)
    found = changes(site)
    assert [(change.action, change.path) for change in reversed(found)] == [
        ("created", "Areas/pruning.md"),
        ("updated", "Areas/pruning.md"),
        ("moved", "Areas/apples.md"),
        ("deleted", "Areas/apples.md"),
    ]
    assert {(change.author_email, change.client) for change in found} == {("bo@example.com", "bo laptop")}
    assert found[1].from_path == "Areas/pruning.md"
    assert all(change.at > 0 for change in found)


@pytest.mark.anyio
async def test_webdav_writes_moves_and_deletes_are_in_the_history_too(site: Hosted):
    ann, bo = await club(site)
    key = key_of(site, "bo@example.com", "Workshop iPad")
    async with ann, bo:
        put = await bo.put("/dav/vault/Areas/pruning.md", content=PRUNING.encode(), headers=basic(key))
        again = await bo.put("/dav/vault/Areas/pruning.md", content=PRUNING_LATER.encode(), headers=basic(key))
        moved = await bo.request(
            "MOVE",
            "/dav/vault/Areas/pruning.md",
            headers={**basic(key), "Destination": f"{BASE}/dav/vault/Archive/pruning.md"},
        )
        gone = await bo.delete("/dav/vault/Archive/pruning.md", headers=basic(key))
        dotted = await bo.put("/dav/vault/.obsidian/app.json", content=b"{}", headers=basic(key))
    assert [put.status_code, again.status_code, moved.status_code, gone.status_code] == [201, 204, 201, 204]
    assert dotted.status_code == 201
    found = changes(site)
    assert [(change.action, change.path) for change in reversed(found)] == [
        ("created", "Areas/pruning.md"),
        ("updated", "Areas/pruning.md"),
        ("moved", "Archive/pruning.md"),
        ("deleted", "Archive/pruning.md"),
    ]
    assert {(change.author_email, change.client) for change in found} == {("bo@example.com", "Workshop iPad")}


@pytest.mark.anyio
async def test_restore_writes_the_previous_version_back_as_a_new_change(site: Hosted):
    ann, bo = await club(site)
    key = key_of(site, "bo@example.com", "bo laptop")
    async with ann, bo:
        await write(bo, key, "Areas/pruning.md", PRUNING)
        await write(bo, key, "Areas/pruning.md", PRUNING_LATER)
        update = changes(site)[0]
        shown = await ann.get(f"/account/vaults/orchardclub/changes/{update.id}")
        history = await ann.get("/account/vaults/orchardclub/history")
        restored = await ann.post(
            f"/account/vaults/orchardclub/changes/{update.id}/restore", data={"csrf": csrf_of(shown.text)}
        )
        read = await call_tool(bo, key, "read_file", {"path": "Areas/pruning.md"}, "/mcp?vault=orchardclub")
    assert shown.status_code == 200
    assert '<span class="del">-Prune the apple trees in March.</span>' in shown.text
    assert '<span class="add">+Prune the apple trees in late February.</span>' in shown.text
    assert "Restore previous" in shown.text
    assert history.text.count('<li><span class="what">Areas/pruning.md</span>') == 2
    assert restored.status_code == 200 and "Restored Areas/pruning.md" in restored.text
    assert "in March" in text_of(read) and "late February" not in text_of(read)
    newest = changes(site)[0]
    assert (newest.action, newest.author_email, newest.client) == ("updated", "ann@example.com", "the vault's web page")
    assert newest.restored_from == update.id and len(changes(site)) == 3
    assert "restored by you" in restored.text


@pytest.mark.anyio
async def test_previous_versions_are_sealed_and_go_with_the_vault(site: Hosted, storage: Path):
    ann, bo = await club(site)
    key = key_of(site, "bo@example.com", "bo laptop")
    vault = club_vault(site)
    async with ann, bo:
        await write(bo, key, "Areas/pruning.md", PRUNING)
        await write(bo, key, "Areas/pruning.md", PRUNING_LATER)
        history_root = storage / "users" / "history" / vault.id
        stored = [path.read_bytes() for path in history_root.rglob("*") if path.is_file()]
        with sqlite3.connect(site.store.database.path) as connection:
            paths = [row[0] for row in connection.execute("SELECT path FROM vault_changes")]
        page = await ann.get("/account/vaults/orchardclub")
        deleted = await ann.post(
            "/account/vaults/orchardclub/delete", data={"csrf": csrf_of(page.text), "name": "Orchard club"}
        )
    assert len(stored) == 2 and not any(b"Prune" in blob for blob in stored)
    assert paths and not any("pruning" in path for path in paths)
    assert deleted.status_code == 200 and "deleted for every member" in deleted.text
    assert not history_root.exists()
    with sqlite3.connect(site.store.database.path) as connection:
        for table in ("vault_changes", "vault_members", "vault_invitations"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


# --- reporting and the shell ----------------------------------------------------------------------------------


def test_the_shell_makes_a_shared_vault_and_manages_its_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], storage: Path
):
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.setenv("VAULT_HOSTED_MASTER_KEY", MasterKey.generate().encoded())
    for name in ("VAULT_PUBLIC_URL", "VAULT_SMTP_HOST"):
        monkeypatch.delenv(name, raising=False)
    store = HostedStore(tmp_path / "auth")
    store.invite("ann@example.com")
    bo, _ = store.invite("bo@example.com")
    assert main(["accounts", "add-vault", "ann@example.com", "Workshop", "--shared"]) == 0
    assert main(["accounts", "add-vault", "ann@example.com", "Shed", "--shared", "--preset", "para"]) == 2
    assert main(["accounts", "add-member", "ann@example.com", "workshop", "bo@example.com"]) == 0
    assert main(["accounts", "add-member", "ann@example.com", "ann", "bo@example.com"]) == 1
    with store.connect() as connection:
        from mindin.auth.hosted_vaults import accept_invitations

        accept_invitations(connection, bo.id, ("bo@example.com",))
    assert main(["accounts", "members", "ann@example.com", "workshop"]) == 0
    listing = capsys.readouterr().out
    assert main(["accounts", "vaults", "bo@example.com"]) == 0
    vaults = capsys.readouterr().out
    assert main(["accounts", "remove-member", "ann@example.com", "workshop", "bo@example.com"]) == 0
    removed = capsys.readouterr().out
    assert "ann@example.com has the shared vault Workshop at /mcp?vault=workshop" in listing
    assert "invited bo@example.com to workshop; no email was sent" in listing
    assert "ann@example.com  owner" in listing and "bo@example.com  member" in listing
    assert "workshop  Workshop  shared, member" in vaults
    assert "removed bo@example.com from workshop" in removed
    assert (
        store.role(
            store.vault_at(bo.id, "workshop") or store.own_vault(store.account("ann@example.com").id, "workshop"), bo.id
        )
        is None
    )


# --- storage --------------------------------------------------------------------------------------------------


def test_the_migration_adds_shared_vaults_and_leaves_a_personal_vault_as_it_was(storage: Path, tmp_path: Path):
    from test_hosted_vaults import ORCHARD_NOTE, legacy_store

    from mindin.auth.hosted_vaults import to_vaults

    keyring = Keyring(MasterKey.generate())
    account_id, _ = legacy_store(tmp_path / "auth", keyring, {"Areas/trees.md": ORCHARD_NOTE})
    path = tmp_path / "auth" / "hosted.sqlite"
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        to_vaults(connection)
        connection.execute("PRAGMA user_version = 1")
    with sqlite3.connect(path) as connection:
        vaults_before = connection.execute("SELECT * FROM vaults").fetchall()
        keys_before = connection.execute("SELECT * FROM vault_data_keys").fetchall()
    raw_before = {file: file.read_bytes() for file in storage.rglob("*") if file.is_file()}
    SqliteDatabase(path).migrate(MIGRATIONS)
    SqliteDatabase(path).migrate(MIGRATIONS)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        after = connection.execute("SELECT id, account_id, name, created_at, slug FROM vaults").fetchall()
        assert [tuple(row)[:4] for row in after] == [(row[0], row[1], row[2], row[4]) for row in vaults_before]
        assert [row[4] for row in after] == ["notes"]
        assert connection.execute("SELECT kind FROM vaults").fetchall() == [("personal",)]
        assert connection.execute("SELECT * FROM vault_data_keys").fetchall() == keys_before
        for table in ("vault_members", "vault_invitations", "vault_changes"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert {file: file.read_bytes() for file in storage.rglob("*") if file.is_file()} == raw_before
    store = HostedStore(tmp_path / "auth")
    vault = store.first_vault(account_id)
    assert vault is not None and not vault.shared
    backend, _ = SealedVaults(store, VaultKeys(store, keyring)).for_vault(vault.id)
    assert backend.get("Areas/trees.md")[0] == ORCHARD_NOTE


@pytest.mark.anyio
async def test_an_invitation_to_a_slug_the_member_already_reaches_waits_until_they_rename_theirs(site: Hosted):
    ann, bo = await club(site)
    made = await make_shared(site, ann, "Notes")
    assert made.headers["location"] == "/account/vaults/notes"
    shared = club_vault(site, "notes")
    assert shared is not None and shared.account_id == site.store.account("ann@example.com").id
    site.store.invite_member(shared, "bo@example.com", shared.account_id)
    bo_id = site.store.account("bo@example.com").id
    async with ann, bo:
        waiting = await bo.get("/account")
        before = site.store.role(shared, bo_id)
        site.store.rename_vault(site.store.own_vault(bo_id, "notes").id, slug="mine")
        joined = await bo.get("/account")
    assert "You are invited to Notes, but you already reach Personal at the address <code>notes</code>" in waiting.text
    assert "Rename Personal" in waiting.text and before is None
    assert site.store.role(shared, bo_id) == "member" and "You are invited" not in joined.text


@pytest.mark.anyio
async def test_a_rename_refuses_a_slug_a_member_already_reaches(site: Hosted):
    ann, bo = await club(site)
    async with ann, bo:
        pass
    with pytest.raises(
        VaultNameError, match="A member of Orchard club already reaches another vault at the address notes"
    ):
        site.store.rename_vault(club_vault(site).id, slug="notes")
    assert site.store.rename_vault(club_vault(site).id, slug="club").slug == "club"


@pytest.mark.anyio
async def test_a_hand_over_refuses_an_owner_who_already_owns_the_slug(site: Hosted):
    ann, bo = await club(site)
    async with ann, bo:
        pass
    bo_id = site.store.account("bo@example.com").id
    with site.store.connect() as connection:
        connection.execute(
            "INSERT INTO vaults(id, account_id, name, slug, created_at, kind) VALUES('vlt_twin', ?, 'Twin',"
            " 'orchardclub', 1, 'personal')",
            (bo_id,),
        )
    with pytest.raises(MembershipError, match="already reaches another vault at the address orchardclub"):
        site.store.hand_over(club_vault(site), bo_id)
    assert club_vault(site).account_id == site.store.account("ann@example.com").id
