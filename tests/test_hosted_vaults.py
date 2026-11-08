from __future__ import annotations

import base64
import io
import shutil
import sqlite3
import zipfile
from pathlib import Path

import httpx2
import pytest
from hosted_support import (
    BASE,
    INITIALIZE,
    NOTE_BODY,
    Hosted,
    call_tool,
    csrf_of,
    hosted,
    post_mcp,
    query,
    text_of,
)
from test_hosted_app import authorize, consent, register
from test_signin_device import decide, poll, start

from mindin.backends import make_subject_backend
from mindin.backends.local import LocalBackend
from mindin.backends.sealed import SealedBackend, Unreadable, new_data_key
from mindin.cli import main
from mindin.hosted.accounts.access import SupportAccess
from mindin.hosted.accounts.keys import Keyring, MasterKey, SealedVaults, VaultKeys
from mindin.hosted.accounts.store import HostedStore, digest
from mindin.hosted.database import SqliteDatabase
from mindin.hosted.pages.web import RECENT_SIGN_IN
from mindin.hosted.schema import STEPS, STORE_TABLES
from mindin.vault import SubjectVaults

STORE_SCRIPT = "".join(f"{statement};\n" for statement in STORE_TABLES)
FRONT = "date: 2026-09-01\nupdated: 2026-09-01\nstatus: active\n"
ORCHARD_NOTE = f"---\ntitle: Apple trees\n{FRONT}tags: [orchard]\n---\n\nPrune in March.\n"
WORKSHOP_NOTE = (
    "---\ntitle: Lathe\ndate: 2026-09-01\nupdated: 2026-09-01\ntags: [workshop]\nstatus: active\n---\n\nOil the bed.\n"
)
LEGACY_TABLES = """
CREATE TABLE vault_keys (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id),
    key_id TEXT NOT NULL,
    wrapped BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    rewrapped_at INTEGER
);
CREATE TABLE support_grants (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id),
    granted_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE vault_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    at INTEGER NOT NULL,
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    action TEXT NOT NULL
);
"""


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


def basic(secret: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"ann@example.com:{secret}".encode()).decode()}


async def two_vaults(site: Hosted) -> httpx2.AsyncClient:
    http = await site.member("ann@example.com", sub="sub-ann")
    account = site.store.account("ann@example.com")
    site.store.rename_vault(site.vault_of("ann@example.com"), "Orchard", "orchard")
    site.store.create_vault(account.id, "Workshop")
    orchard, workshop = (
        site.key_for("ann@example.com", vault="orchard"),
        site.key_for("ann@example.com", vault="workshop"),
    )
    for key, note in ((orchard, ORCHARD_NOTE), (workshop, WORKSHOP_NOTE)):
        written = await call_tool(http, key, "write_file", {"path": "Areas/here.md", "content": note})
        assert not written.get("isError"), text_of(written)
    return http


# --- keys and WebDAV ----------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_key_reaches_only_its_own_vault_of_the_account(site: Hosted):
    http = await two_vaults(site)
    orchard = site.key_for("ann@example.com", vault="orchard", name="orchard laptop")
    async with http:
        plain = await call_tool(http, orchard, "read_file", {"path": "Areas/here.md"})
        bound = await call_tool(http, orchard, "read_file", {"path": "Areas/here.md"}, path="/mcp?vault=orchard")
        refused = await post_mcp(http, orchard, INITIALIZE, path="/mcp?vault=workshop")
        unknown = await post_mcp(http, orchard, INITIALIZE, path="/mcp?vault=nowhere")
    assert "Prune in March" in text_of(plain) and "Prune in March" in text_of(bound)
    assert refused.status_code == 403 and refused.json()["error"] == "wrong_vault"
    assert (unknown.status_code, unknown.json()) == (refused.status_code, refused.json())


@pytest.mark.anyio
async def test_webdav_opens_the_vault_the_key_was_made_for(site: Hosted):
    http = await two_vaults(site)
    workshop = site.key_for("ann@example.com", vault="workshop", name="obsidian")
    async with http:
        got = await http.get("/dav/vault/Areas/here.md", headers=basic(workshop))
    assert got.status_code == 200 and b"Oil the bed" in got.content


@pytest.mark.anyio
async def test_obsidian_access_mints_a_webdav_key_for_the_connections_own_vault(site: Hosted):
    http = await two_vaults(site)
    workshop = site.key_for("ann@example.com", vault="workshop")
    async with http:
        minted = await call_tool(http, workshop, "obsidian_access", {}, path="/mcp?vault=workshop")
        password = text_of(minted).split("Password: ")[1].split()[0]
        got = await http.get("/dav/vault/Areas/here.md", headers=basic(password))
    assert b"Oil the bed" in got.content


# --- OAuth ----------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_vault_address_is_its_own_protected_resource(site: Hosted):
    await two_vaults(site)
    async with site.browser() as http:
        challenged = await http.post("/mcp?vault=workshop", json={})
        metadata = await http.get("/.well-known/oauth-protected-resource/mcp?vault=workshop")
        server = await http.get("/.well-known/oauth-protected-resource/mcp")
        missing = await http.get("/.well-known/oauth-protected-resource/mcp?vault=nowhere")
        unknown = await http.post("/mcp?vault=nowhere", json={})
        stale = await http.post("/mcp?vault=nowhere", json={}, headers={"Authorization": "Bearer frame-token"})
        known_stale = await http.post("/mcp?vault=workshop", json={}, headers={"Authorization": "Bearer frame-token"})
    assert challenged.status_code == 401
    challenge = challenged.headers["www-authenticate"]
    assert 'resource_metadata="http://localhost/.well-known/oauth-protected-resource/mcp?vault=workshop"' in challenge
    assert metadata.json()["resource"] == f"{BASE}/mcp?vault=workshop"
    assert server.json() == {**metadata.json(), "resource": f"{BASE}/mcp"}
    assert metadata.json()["authorization_servers"] == [BASE]
    assert missing.status_code == metadata.status_code
    assert missing.json() == {**metadata.json(), "resource": f"{BASE}/mcp?vault=nowhere"}
    assert missing.headers["cache-control"] == metadata.headers["cache-control"]
    assert unknown.status_code == 401
    assert unknown.headers["www-authenticate"] == challenge.replace("vault=workshop", "vault=nowhere")
    assert unknown.json() == challenged.json()
    assert (stale.status_code, stale.json()) == (known_stale.status_code, known_stale.json())


async def oauth(http: httpx2.AsyncClient, resource: str, vault: str = "") -> tuple[httpx2.Response, dict, str]:
    client = await register(http)
    verifier, started = await authorize(http, client["client_id"], resource=resource)
    assert started.status_code == 302, started.text
    page, approved = await consent(http, started.headers["location"], vault=vault)
    assert approved.status_code == 302, approved.text
    response = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query(approved.headers["location"])["code"],
            "redirect_uri": "http://localhost:9876/callback",
            "client_id": client["client_id"],
            "code_verifier": verifier,
            "resource": resource,
        },
    )
    assert response.status_code == 200, response.text
    return page, response.json(), client["client_id"]


@pytest.mark.anyio
async def test_oauth_through_a_vault_address_gets_only_that_vault(site: Hosted):
    http = await two_vaults(site)
    async with http:
        page, tokens, client_id = await oauth(http, f"{BASE}/mcp?vault=workshop")
        access = tokens["access_token"]
        bound = await call_tool(http, access, "read_file", {"path": "Areas/here.md"}, path="/mcp?vault=workshop")
        elsewhere = await post_mcp(http, access, INITIALIZE, path="/mcp?vault=orchard")
        unknown = await post_mcp(http, access, INITIALIZE, path="/mcp?vault=nowhere")
        plain = await post_mcp(http, access, INITIALIZE)
        refreshed = await http.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id},
        )
        again = await call_tool(
            http, refreshed.json()["access_token"], "read_file", {"path": "Areas/here.md"}, path="/mcp?vault=workshop"
        )
    assert 'type="radio" name="vault"' not in page.text, "the vault is fixed by the address the client came through"
    assert "Workshop" in page.text
    assert "Oil the bed" in text_of(bound) and "Oil the bed" in text_of(again)
    assert elsewhere.status_code == 401 and plain.status_code == 401
    assert (unknown.status_code, unknown.json()) == (elsewhere.status_code, elsewhere.json())


@pytest.mark.anyio
async def test_a_client_revokes_an_access_token_it_got_for_a_vault_address(site: Hosted):
    http = await two_vaults(site)
    async with http:
        _, tokens, client_id = await oauth(http, f"{BASE}/mcp?vault=workshop")
        revoked = await http.post(
            "/revoke", data={"token": tokens["access_token"], "client_id": client_id, "client_secret": ""}
        )
        refused = await post_mcp(http, tokens["access_token"], INITIALIZE, path="/mcp?vault=workshop")
    assert revoked.status_code == 200 and refused.status_code == 401


@pytest.mark.anyio
async def test_oauth_through_the_server_address_lets_the_user_pick_the_vault(site: Hosted, vaults: SubjectVaults):
    http = await two_vaults(site)
    async with http:
        page, tokens, _ = await oauth(http, f"{BASE}/mcp", vault="workshop")
        read = await call_tool(http, tokens["access_token"], "read_file", {"path": "Areas/here.md"})
        wrote = await call_tool(
            http, tokens["access_token"], "write_file", {"path": "Areas/new.md", "content": NOTE_BODY}
        )
        listed = await http.get("/account/vaults/workshop")
        named = await call_tool(
            http, tokens["access_token"], "read_file", {"path": "Areas/here.md"}, "/mcp?vault=workshop"
        )
        elsewhere = await post_mcp(http, tokens["access_token"], INITIALIZE, path="/mcp?vault=orchard")
    assert page.text.count('type="radio" name="vault"') == 2
    assert "Oil the bed" in text_of(read) and not wrote.get("isError")
    assert "Oil the bed" in text_of(named) and elsewhere.status_code == 401
    assert "Cursor" in listed.text and "Connector" in listed.text and "Disconnect" in listed.text
    orchard = vaults.backend_factory(site.vault_of("ann@example.com", "orchard"))[0]
    assert "Areas/new.md" not in {entry.key for entry in orchard.list_all()}


@pytest.mark.anyio
async def test_a_client_that_drops_the_vault_from_its_resource_gets_the_picker_and_the_vault_it_picks(site: Hosted):
    http = await two_vaults(site)
    async with http:
        challenged = await http.post("/mcp?vault=workshop", json={})
        page, picked, _ = await oauth(http, f"{BASE}/mcp", vault="workshop")
        _, other, _ = await oauth(http, f"{BASE}/mcp", vault="orchard")
        reached = await call_tool(
            http, picked["access_token"], "read_file", {"path": "Areas/here.md"}, "/mcp?vault=workshop"
        )
        refused = await post_mcp(http, other["access_token"], INITIALIZE, path="/mcp?vault=workshop")
    assert challenged.status_code == 401
    assert page.text.count('type="radio" name="vault"') == 2
    assert "Oil the bed" in text_of(reached)
    assert refused.status_code == 401 and refused.json()["error_description"] == "the token belongs to another vault"


@pytest.mark.anyio
async def test_the_consent_page_refuses_a_vault_the_address_did_not_name(site: Hosted):
    http = await two_vaults(site)
    async with http:
        client = await register(http)
        _, started = await authorize(http, client["client_id"], resource=f"{BASE}/mcp?vault=workshop")
        request_id = query(started.headers["location"])["request"]
        page = await http.get("/login", params={"request": request_id})
        swapped = await http.post(
            "/login",
            data={"request": request_id, "csrf": csrf_of(page.text), "vault": "orchard", "decision": "approve"},
        )
        _, unknown = await authorize(http, client["client_id"], resource=f"{BASE}/mcp?vault=nowhere")
        unknown_page = await http.get("/login", params={"request": query(unknown.headers["location"])["request"]})
        _, extra = await authorize(http, client["client_id"], resource=f"{BASE}/mcp?vault=workshop&other=1")
        _, path = await authorize(http, client["client_id"], resource=f"{BASE}/v/workshop/mcp")
    assert 'type="radio" name="vault"' not in page.text and "Workshop" in page.text
    assert swapped.status_code == 200 and "Choose one of your vaults." in swapped.text
    assert unknown_page.status_code == 403 and "does not own" in unknown_page.text
    assert "invalid_target" in extra.headers["location"] and "invalid_target" in path.headers["location"]


@pytest.mark.anyio
async def test_a_vault_address_of_another_account_is_refused_at_consent(site: Hosted):
    await two_vaults(site)
    bo = await site.member("bo@example.com", sub="sub-bo")
    async with bo:
        client = await register(bo)
        _, started = await authorize(bo, client["client_id"], resource=f"{BASE}/mcp?vault=workshop")
        page = await bo.get("/login", params={"request": query(started.headers["location"])["request"]})
        _, unknown = await authorize(bo, client["client_id"], resource=f"{BASE}/mcp?vault=nowhere")
        unknown_page = await bo.get("/login", params={"request": query(unknown.headers["location"])["request"]})
    assert page.status_code == 403 and "does not own" in page.text
    assert (unknown_page.status_code, unknown_page.text) == (page.status_code, page.text)


@pytest.mark.anyio
async def test_two_accounts_each_reach_their_own_vault_of_the_same_slug(site: Hosted):
    ann = await two_vaults(site)
    bo = await site.member("bo@example.com", sub="sub-bo")
    site.store.create_vault(site.store.account("bo@example.com").id, "Workshop")
    bo_key = site.key_for("bo@example.com", vault="workshop")
    async with ann, bo:
        written = await call_tool(bo, bo_key, "write_file", {"path": "Areas/here.md", "content": ORCHARD_NOTE})
        _, ann_tokens, _ = await oauth(ann, f"{BASE}/mcp?vault=workshop")
        _, bo_tokens, _ = await oauth(bo, f"{BASE}/mcp?vault=workshop")
        ann_read = await call_tool(
            ann, ann_tokens["access_token"], "read_file", {"path": "Areas/here.md"}, "/mcp?vault=workshop"
        )
        bo_read = await call_tool(
            bo, bo_tokens["access_token"], "read_file", {"path": "Areas/here.md"}, "/mcp?vault=workshop"
        )
        by_key = await call_tool(bo, bo_key, "read_file", {"path": "Areas/here.md"}, "/mcp?vault=workshop")
    assert not written.get("isError")
    assert "Oil the bed" in text_of(ann_read)
    assert "Prune in March" in text_of(bo_read) and "Prune in March" in text_of(by_key)


@pytest.mark.anyio
async def test_disconnecting_a_connector_stops_its_tokens(site: Hosted):
    http = await two_vaults(site)
    async with http:
        _, tokens, _ = await oauth(http, f"{BASE}/mcp?vault=workshop")
        page = await http.get("/account/vaults/workshop")
        client_id = page.text.split('name="client" value="')[1].split('"')[0]
        gone = await http.post(
            "/account/vaults/workshop/connections/disconnect", data={"csrf": csrf_of(page.text), "client": client_id}
        )
        refused = await post_mcp(http, tokens["access_token"], INITIALIZE, path="/mcp?vault=workshop")
        after = await http.get("/account/vaults/workshop")
    assert gone.status_code == 303 and refused.status_code == 401
    assert "Disconnect" not in after.text


# --- device flow ------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_device_login_for_a_named_vault_gets_a_key_to_that_vault(site: Hosted):
    member = await two_vaults(site)
    async with site.browser() as machine, member:
        device = await start(machine, vault="workshop")
        page, _ = await decide(member, device["user_code"], vault="workshop")
        granted = (await poll(site, machine, device)).json()
        read = await call_tool(machine, granted["access_token"], "read_file", {"path": "Areas/here.md"})
        signed_in = await member.get("/account/vaults/workshop")
    assert 'type="radio" name="vault"' not in page.text and "Workshop" in page.text
    assert granted["vault"] == "workshop" and granted["vault_name"] == "Workshop"
    assert "Oil the bed" in text_of(read)
    assert "devbox" in signed_in.text and "Computer" in signed_in.text and "Sign out" in signed_in.text


@pytest.mark.anyio
async def test_a_device_login_without_a_vault_asks_which_one(site: Hosted):
    member = await two_vaults(site)
    async with site.browser() as machine, member:
        device = await start(machine)
        page, _ = await decide(member, device["user_code"], vault="orchard")
        granted = (await poll(site, machine, device)).json()
    assert page.text.count('type="radio" name="vault"') == 2
    assert granted["vault"] == "orchard"


@pytest.mark.anyio
async def test_a_device_login_for_someone_elses_vault_is_refused(site: Hosted):
    await two_vaults(site)
    bo = await site.member("bo@example.com", sub="sub-bo")
    async with site.browser() as machine, bo:
        device = await start(machine, vault="workshop")
        page = await bo.get("/device", params={"user_code": device["user_code"]})
    assert page.status_code == 403 and "not one of yours" in page.text


# --- pages ---------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_vault_list_shows_each_vault_with_its_address(site: Hosted):
    http = await two_vaults(site)
    async with http:
        listed = await http.get("/account")
        page = await http.get("/account/vaults/workshop")
        other = await site.member("bo@example.com", sub="sub-bo")
        async with other:
            foreign = await other.get("/account/vaults/workshop")
    assert listed.text.index("Orchard") < listed.text.index("Workshop")
    assert "…/mcp?vault=orchard" in listed.text and "…/mcp?vault=workshop" in listed.text
    assert "2 notes" in listed.text and "1 key," in listed.text
    assert f"{BASE}/mcp?vault=workshop" in page.text
    assert "mindin login http://localhost --vault workshop" in page.text
    assert f"{BASE}/dav/" in page.text
    assert foreign.status_code == 404


@pytest.mark.anyio
async def test_a_key_made_on_a_vault_page_reaches_that_vault(site: Hosted):
    http = await two_vaults(site)
    async with http:
        page = await http.get("/account/vaults/workshop")
        made = await http.post(
            "/account/vaults/workshop/keys", data={"csrf": csrf_of(page.text), "name": "phone", "access": "read"}
        )
        key = made.text.split('<code id="new-key">')[1].split("<")[0]
        read = await call_tool(http, key, "read_file", {"path": "Areas/here.md"})
        wrote = await call_tool(http, key, "write_file", {"path": "Areas/x.md", "content": NOTE_BODY})
    assert "Oil the bed" in text_of(read)
    assert wrote.get("isError")


@pytest.mark.anyio
async def test_a_vault_exports_and_imports_only_its_own_files(site: Hosted):
    http = await two_vaults(site)
    async with http:
        exported = await http.get("/account/vaults/workshop/export")
    assert exported.status_code == 200
    assert 'filename="workshop-' in exported.headers["content-disposition"]
    assert b"Oil the bed" in _unzipped(exported.content) and b"Prune in March" not in _unzipped(exported.content)


def _unzipped(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return b"".join(archive.read(name) for name in archive.namelist())


# --- deleting a vault ------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_deleting_a_vault_leaves_the_others_and_its_own_notes_unreadable(
    site: Hosted, vaults: SubjectVaults, storage: Path, tmp_path: Path
):
    http = await two_vaults(site)
    workshop_id = site.vault_of("ann@example.com", "workshop")
    orchard_id = site.vault_of("ann@example.com", "orchard")
    workshop_key = site.key_for("ann@example.com", vault="workshop")
    orchard_key = site.key_for("ann@example.com", vault="orchard")
    snapshot = tmp_path / "snapshot"
    shutil.copytree(storage / "users" / workshop_id, snapshot)
    async with http:
        page = await http.get("/account/vaults/workshop")
        wrong = await http.post("/account/vaults/workshop/delete", data={"csrf": csrf_of(page.text), "name": "Orchard"})
        deleted = await http.post(
            "/account/vaults/workshop/delete", data={"csrf": csrf_of(page.text), "name": "Workshop"}
        )
        refused = await post_mcp(http, workshop_key, INITIALIZE)
        still = await call_tool(http, orchard_key, "read_file", {"path": "Areas/here.md"})
    assert wrong.status_code == 400 and "Type the vault&#x27;s name" in wrong.text
    assert deleted.status_code == 200 and "Workshop is deleted" in deleted.text
    assert refused.status_code == 401
    assert "Prune in March" in text_of(still)
    assert not (storage / "users" / workshop_id).exists()
    keys = VaultKeys(site.store, site.keyring)
    assert not keys.has_key(workshop_id) and keys.has_key(orchard_id)
    blobs = [path.read_bytes() for path in snapshot.rglob("*") if path.is_file()]
    assert blobs
    for data_key in (keys.data_key(orchard_id), new_data_key()):
        revived = SealedBackend(LocalBackend(snapshot), data_key)
        assert revived.list_all() == []
        for blob in blobs:
            with pytest.raises(Unreadable):
                revived.unseal("Areas/here.md", blob)


@pytest.mark.anyio
async def test_deleting_a_vault_needs_a_recent_sign_in(site: Hosted):
    http = await two_vaults(site)
    async with http:
        page = await http.get("/account/vaults/workshop")
        site.clock.advance(RECENT_SIGN_IN + 1)
        stale = await http.post(
            "/account/vaults/workshop/delete", data={"csrf": csrf_of(page.text), "name": "Workshop"}
        )
    assert stale.status_code == 403 and "Sign in again" in stale.text
    assert 'value="/account/vaults/workshop"' in stale.text
    assert site.store.own_vault(site.store.account("ann@example.com").id, "workshop") is not None


@pytest.mark.anyio
async def test_deleting_the_account_deletes_every_vault(site: Hosted, storage: Path):
    http = await two_vaults(site)
    ids = [vault.id for vault in site.store.vaults(site.store.account("ann@example.com").id)]
    async with http:
        form = await http.get("/account/delete")
        deleted = await http.post("/account/delete", data={"csrf": csrf_of(form.text), "email": "ann@example.com"})
    assert deleted.status_code == 200
    assert "Orchard: " in form.text and "Workshop: " in form.text
    assert all(not (storage / "users" / vault_id).exists() for vault_id in ids)
    with sqlite3.connect(site.store.database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM vaults").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM vault_data_keys").fetchone()[0] == 0


# --- support access, CLI ---------------------------------------------------------------------------------------


@pytest.fixture
def cli_env(site: Hosted, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_AUTH_DIR", str(site.config.auth_dir))
    monkeypatch.setenv("VAULT_HOSTED_MASTER_KEY", site.keyring.current.encoded())


@pytest.mark.anyio
async def test_support_access_is_granted_per_vault(site: Hosted, cli_env: None, capsys: pytest.CaptureFixture[str]):
    http = await two_vaults(site)
    support = ["--operator", "sam", "--reason", "ticket 12"]
    async with http:
        page = await http.get("/account/settings")
        granted = await http.post(
            "/account/support", data={"csrf": csrf_of(page.text), "vault": "orchard", "duration": "hour"}
        )
        after = await http.get("/account/settings")
    assert granted.status_code == 303
    assert "Orchard: open to support until" in after.text
    assert main(["support", "files", "ann@example.com", *support]) == 1
    assert "--vault" in capsys.readouterr().err
    assert main(["support", "files", "ann@example.com", "--vault", "workshop", *support]) == 1
    assert "has no support grant" in capsys.readouterr().err
    assert main(["support", "read", "ann@example.com", "Areas/here.md", "--vault", "orchard", *support]) == 0
    assert "Prune in March" in capsys.readouterr().out
    rows = SupportAccess(site.store).log(site.store.account("ann@example.com").id)
    assert [row["vault_name"] for row in rows] == ["Orchard"]


@pytest.mark.anyio
async def test_the_cli_lists_adds_and_renames_vaults(site: Hosted, cli_env: None, capsys: pytest.CaptureFixture[str]):
    await two_vaults(site)
    assert main(["accounts", "add-vault", "ann@example.com", "Greenhouse", "--preset", "para"]) == 0
    assert "at /mcp?vault=greenhouse" in capsys.readouterr().out
    assert main(["accounts", "add-vault", "ann@example.com", "Orchard", "--slug", "orchard"]) == 2
    assert "already reach the vault Orchard at the address orchard" in capsys.readouterr().err
    for wrong in ("glass-house", "glass.house", "glass_house", "Glasshouse", "9house", "new", "g" * 33):
        assert main(["accounts", "add-vault", "ann@example.com", "Shed", "--slug", wrong]) == 2
        assert "one word of lowercase letters" in capsys.readouterr().err
        assert main(["accounts", "rename-vault", "ann@example.com", "greenhouse", "--slug", wrong]) == 2
        assert "one word of lowercase letters" in capsys.readouterr().err
    assert main(["accounts", "rename-vault", "ann@example.com", "greenhouse", "--slug", "workshop"]) == 2
    assert "already reach the vault Workshop" in capsys.readouterr().err
    assert main(["accounts", "rename-vault", "ann@example.com", "greenhouse", "--slug", "glasshouse"]) == 0
    assert "is at /mcp?vault=glasshouse" in capsys.readouterr().out
    assert main(["accounts", "vaults", "ann@example.com"]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert [line.split("  ")[0] for line in listed[-3:]] == ["orchard", "workshop", "glasshouse"]
    assert "2 notes" in listed[-3]


# --- the migration ---------------------------------------------------------------------------------------------


def legacy_store(folder: Path, keyring: Keyring, notes: dict[str, str]) -> tuple[str, str]:
    path = folder / "hosted.sqlite"
    folder.mkdir(parents=True, exist_ok=True)
    account_id = "acc_orchardkeeper00000001"
    with sqlite3.connect(path) as connection:
        connection.executescript(STORE_SCRIPT + LEGACY_TABLES)
        connection.execute(
            "INSERT INTO accounts(id, email, sub, status, status_changed_at, created_at)"
            " VALUES(?, 'ann@example.com', 'sub-ann', 'active', 1, 1)",
            (account_id,),
        )
        key_id, wrapped = keyring.wrap(account_id, data_key := new_data_key())
        connection.execute(
            "INSERT INTO vault_keys(account_id, key_id, wrapped, created_at) VALUES(?, ?, ?, 1)",
            (account_id, key_id, wrapped),
        )
        connection.execute(
            "INSERT INTO support_grants(account_id, granted_at, expires_at) VALUES(?, 1, 4102444800)", (account_id,)
        )
        connection.execute(
            "INSERT INTO vault_access(account_id, at, operator, reason, action)"
            " VALUES(?, 2, 'sam', 'move in', 'import')",
            (account_id,),
        )
    secret = "nvm_0123456789ab" + "s" * 43
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO api_keys(id, account_id, name, key_hash, scopes, purpose, created_at)"
            " VALUES('0123456789ab', ?, 'laptop', ?, 'vault:read vault:write', 'device', 1)",
            (account_id, digest(secret)),
        )
    inner, _ = make_subject_backend(account_id, "users")
    sealed = SealedBackend(inner, data_key)
    for name, text in notes.items():
        sealed.put(name, text)
    return account_id, secret


@pytest.mark.anyio
async def test_the_migration_keeps_an_existing_vault_where_it_is_and_readable(
    storage: Path, tmp_path: Path, vaults: SubjectVaults
):
    keyring = Keyring(MasterKey.generate())
    notes = {f"Areas/tree-{number}.md": ORCHARD_NOTE.replace("Apple", f"Apple {number}") for number in range(40)}
    account_id, secret = legacy_store(tmp_path / "auth", keyring, notes)
    raw_before = {path: path.read_bytes() for path in storage.rglob("*") if path.is_file()}
    store = HostedStore(tmp_path / "auth")
    store.database.migrate(STEPS)
    HostedStore(tmp_path / "auth")
    with sqlite3.connect(store.database.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == len(STEPS)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'vault_keys'").fetchone() is None
        assert connection.execute("SELECT COUNT(*) FROM vault_data_keys").fetchone()[0] == 1
    vault = store.first_vault(account_id)
    assert vault is not None and vault.id == account_id and vault.slug == "notes" and len(store.vaults(account_id)) == 1
    sealed = SealedVaults(store, VaultKeys(store, keyring))
    backend, _ = sealed.for_vault(vault.id)
    assert {entry.key: backend.get(entry.key)[0] for entry in backend.list()} == notes
    assert {path: path.read_bytes() for path in storage.rglob("*") if path.is_file()} == raw_before
    assert SupportAccess(store).grant_until(vault.id) == 4102444800
    assert [row["vault_name"] for row in SupportAccess(store).log(account_id)] == ["Personal"]
    async with hosted(vaults, tmp_path, keyring=keyring) as site, site.browser() as http:
        read = await call_tool(http, secret, "read_file", {"path": "Areas/tree-7.md"})
        listed = await call_tool(http, secret, "list_files", {})
    assert "Apple 7 trees" in text_of(read)
    assert len([line for line in text_of(listed).splitlines() if line.startswith("Areas/tree-")]) == 40


def test_a_migration_that_cannot_move_every_key_changes_nothing(
    storage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from mindin.hosted import schema

    keyring = Keyring(MasterKey.generate())
    legacy_store(tmp_path / "auth", keyring, {})
    real = schema.count
    monkeypatch.setattr(schema, "count", lambda connection, sql: -1 if "JOIN" in sql else real(connection, sql))
    with pytest.raises(schema.MigrationError):
        HostedStore(tmp_path / "auth")
    with sqlite3.connect(tmp_path / "auth" / "hosted.sqlite") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM vault_keys").fetchone()[0] == 1
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'vaults'").fetchone() is None


def test_new_accounts_get_their_first_vault_and_a_free_slug(tmp_path: Path):
    store = HostedStore(tmp_path / "auth")
    first, _ = store.invite("ann.green@example.com")
    second, _ = store.invite("ann.green@example.org")
    assert [vault.slug for vault in store.vaults(first.id)] == ["notes"]
    assert [vault.slug for vault in store.vaults(second.id)] == ["notes"]
    assert store.first_vault(first.id).id.startswith("vlt_")
    names = ["Work", "Work shop!", "Work", "Wörk", "2026 plans", "!!!", "New", "Notes", "x" * 40, "x" * 40]
    made = [store.create_vault(first.id, name).slug for name in names]
    assert made == ["work", "workshop", "work2", "work3", "plans", "vault", "new2", "notes2", "x" * 32, "x" * 31 + "2"]
    assert store.create_vault(second.id, "Work").slug == "work"


def test_one_account_cannot_own_two_vaults_with_one_slug(tmp_path: Path):
    store = HostedStore(tmp_path / "auth")
    ann, _ = store.invite("ann@example.com")
    with store.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO vaults(id, account_id, name, slug, created_at, kind) VALUES('vlt_x', ?, 'Twin', 'notes', 1,"
            " 'personal')",
            (ann.id,),
        )


@pytest.mark.anyio
async def test_the_server_root_sends_a_visitor_to_the_account_page(site: Hosted):
    async with site.browser() as http:
        root = await http.get("/")
    assert root.status_code == 303 and root.headers["location"] == "/account"


def test_the_migration_gives_every_vault_a_one_word_slug_unique_within_what_each_account_reaches(tmp_path: Path):
    folder = tmp_path / "auth"
    folder.mkdir()
    database = SqliteDatabase(folder / "hosted.sqlite")
    with database.connect() as connection:
        connection.executescript(STORE_SCRIPT)
    database.migrate(STEPS[:2])
    vaults = [
        ("v-ann1", "acc-ann", "Personal", "ann-green", 10, "personal"),
        ("v-ann2", "acc-ann", "Work notes", "work.notes", 11, "personal"),
        ("v-ann3", "acc-ann", "2024 Archive", "2024", 12, "personal"),
        ("v-ann4", "acc-ann", "Orchard", "orchard", 13, "shared"),
        ("v-ann5", "acc-ann", "Work", "work", 14, "personal"),
        ("v-ann6", "acc-ann", "Work", "work-2", 15, "personal"),
        ("v-ann7", "acc-ann", "Work", "work-club", 16, "shared"),
        ("v-bo1", "acc-bo", "Personal", "bo-o", 1, "personal"),
        ("v-bo2", "acc-bo", "Work", "work-bo", 2, "personal"),
        ("v-bo3", "acc-bo", "Workbench", "work3", 3, "personal"),
        ("v-bo4", "acc-bo", "Orchard", "orchard-club", 17, "personal"),
    ]
    with database.connect() as connection:
        for account, email in (("acc-ann", "ann@example.com"), ("acc-bo", "bo@example.com")):
            connection.execute(
                "INSERT INTO accounts(id, email, status, status_changed_at, created_at) VALUES(?, ?, 'active', 1, 1)",
                (account, email),
            )
        for row in vaults:
            connection.execute(
                "INSERT INTO vaults(id, account_id, name, slug, created_at, kind) VALUES(?, ?, ?, ?, ?, ?)", row
            )
        for vault in ("v-ann4", "v-ann7"):
            connection.execute(
                "INSERT INTO vault_members(vault_id, account_id, joined_at) VALUES(?, 'acc-bo', 20)", (vault,)
            )
        for token, resource in (("old", f"{BASE}/v/work-club/mcp"), ("plain", f"{BASE}/mcp")):
            connection.execute(
                "INSERT INTO tokens(token_hash, kind, grant_id, client_id, account_id, scopes, resource, issued_at,"
                " expires_at, vault_id) VALUES(?, 'refresh', 'g', 'c', 'acc-ann', 'vault:read', ?, 1, 9999999999,"
                " 'v-ann7')",
                (token, resource),
            )
    store = HostedStore(folder)
    assert {vault.id: vault.slug for vault in store.vaults("acc-ann")} == {
        "v-ann1": "notes",
        "v-ann2": "worknotes",
        "v-ann3": "archive",
        "v-ann4": "orchard",
        "v-ann5": "work",
        "v-ann6": "work2",
        "v-ann7": "work4",
    }
    assert {vault.id: vault.slug for vault in store.vaults("acc-bo")} == {
        "v-bo1": "notes",
        "v-bo2": "work",
        "v-bo3": "work3",
        "v-bo4": "orchard2",
    }
    for account in ("acc-ann", "acc-bo"):
        slugs = [vault.slug for vault in store.reachable_vaults(account)]
        assert len(slugs) == len(set(slugs))
    with store.connect() as connection:
        revoked = dict(connection.execute("SELECT token_hash, revoked FROM tokens").fetchall())
        index = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'vaults_account_slug'").fetchone()[0]
    assert revoked == {"old": 1, "plain": 0}
    assert "UNIQUE INDEX" in index and "(account_id, slug)" in index
