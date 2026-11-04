from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx2
import pytest
from hosted_support import (
    BASE,
    IDP,
    NOTE_BODY,
    Hosted,
    call_tool,
    csrf_of,
    hosted,
    pkce_pair,
    post_mcp,
    query,
    text_of,
    vault_choice_of,
)

from mindin.auth.hosted_store import UNUSED_CLIENT_TTL
from mindin.vault import SubjectVaults

MCP_CIMD = "https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration"
MCP_SECURITY = "https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations"
MCP_AUTH = "https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization"
CLAUDE_AUTH = "https://claude.com/docs/connectors/building/authentication"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CLAUDE_CODE_CIMD = "https://claude.ai/oauth/claude-code-client-metadata"
REDIRECT = "http://localhost:9876/callback"


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


async def register(http: httpx2.AsyncClient, redirect: str = REDIRECT, **extra: Any) -> dict[str, Any]:
    body = {
        "client_name": "Cursor",
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "vault:read vault:write",
        **extra,
    }
    response = await http.post("/register", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def authorize(
    http: httpx2.AsyncClient, client_id: str, redirect: str = REDIRECT, resource: str = f"{BASE}/mcp", **extra: str
) -> tuple[str, httpx2.Response]:
    verifier, challenge = pkce_pair()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "state-7",
        "scope": "vault:read vault:write",
        "resource": resource,
        **extra,
    }
    return verifier, await http.get("/authorize", params=params)


async def consent(
    http: httpx2.AsyncClient, login_url: str, access: str = "write", decision: str = "approve", vault: str = ""
):
    request_id = query(login_url)["request"]
    page = await http.get("/login", params={"request": request_id})
    assert page.status_code == 200, page.text
    form = {
        "request": request_id,
        "csrf": csrf_of(page.text),
        "vault": vault or vault_choice_of(page.text),
        "access": access,
        "decision": decision,
    }
    return page, await http.post("/login", data=form)


async def connect(site: Hosted, http: httpx2.AsyncClient, client_id: str, redirect: str = REDIRECT, **token_extra: str):
    verifier, started = await authorize(http, client_id, redirect)
    assert started.status_code == 302, started.text
    _, approved = await consent(http, started.headers["location"])
    assert approved.status_code == 302, approved.text
    params = query(approved.headers["location"])
    response = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": params["code"],
            "redirect_uri": redirect,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": f"{BASE}/mcp",
            **token_extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- discovery ----------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_metadata_advertises_cimd_dcr_and_the_device_grant(site: Hosted):
    async with site.browser() as http:
        metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["client_id_metadata_document_supported"] is True, MCP_CIMD
    assert "none" in metadata["token_endpoint_auth_methods_supported"], CLAUDE_AUTH
    assert metadata["registration_endpoint"] == f"{BASE}/register"
    assert metadata["device_authorization_endpoint"] == f"{BASE}/device_authorization"
    assert "urn:ietf:params:oauth:grant-type:device_code" in metadata["grant_types_supported"]
    assert metadata["code_challenge_methods_supported"] == ["S256"], MCP_SECURITY
    assert metadata["authorization_response_iss_parameter_supported"] is True, MCP_AUTH
    assert metadata["issuer"] == BASE


@pytest.mark.anyio
async def test_an_unauthenticated_mcp_request_gets_401_pointing_at_the_metadata(site: Hosted):
    async with site.browser() as http:
        response = await http.post("/mcp", json={})
        resource = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert response.status_code == 401, CLAUDE_AUTH
    challenge = response.headers["www-authenticate"]
    assert 'resource_metadata="http://localhost/.well-known/oauth-protected-resource/mcp"' in challenge
    assert 'scope="vault:read vault:write"' in challenge
    assert resource["resource"] == f"{BASE}/mcp"
    assert resource["authorization_servers"] == [BASE]
    assert set(resource["scopes_supported"]) == {"vault:read", "vault:write"}


# --- signing in ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_invited_address_signs_in_and_becomes_active(site: Hosted):
    site.invite("Ann@Example.com")
    async with site.browser() as http:
        first = await http.get("/account")
        assert first.headers["location"] == "/signin?next=%2Faccount"
        chooser = await http.get(first.headers["location"])
        assert "Continue with Pocket ID" in chooser.text
        started = await http.get("/signin/oidc", params={"next": "/account"})
        assert started.headers["location"].startswith(f"{IDP}/authorize?")
        params = query(started.headers["location"])
        assert params["nonce"] and params["state"] and params["code_challenge"]
        state, code = site.idp.approve(started.headers["location"], "sub-ann", "ann@example.com")
        landed = await http.get("/oidc/callback", params={"state": state, "code": code})
        assert landed.status_code == 303
        assert landed.headers["location"] == "/account"
        page = await http.get("/account/settings")
        vault_page = await http.get("/account/vaults/notes")
    assert page.status_code == 200
    assert "ann@example.com" in page.text
    assert f"{BASE}/mcp?vault=notes" in vault_page.text
    assert "<script>" not in page.text + vault_page.text, "scripts load from this server, never inline"
    account = site.store.account("ann@example.com")
    assert account is not None and account.status == "active" and account.sub
    assert site.identities.account_for("oidc", "sub-ann") == account


@pytest.mark.anyio
async def test_an_address_without_an_invite_gets_no_account(site: Hosted):
    async with site.browser() as http:
        refused = await site.sign_in(http, "stranger@example.com")
    assert refused.status_code == 403
    assert "no account" in refused.text
    assert site.store.account("stranger@example.com") is None


@pytest.mark.anyio
async def test_an_unverified_email_does_not_claim_an_invite(site: Hosted):
    site.invite("ann@example.com")
    async with site.browser() as http:
        refused = await site.sign_in(http, "ann@example.com", verified=False)
    assert refused.status_code == 403
    assert "without a verified email" in refused.text
    account = site.store.account("ann@example.com")
    assert account is not None and account.sub is None and account.status == "invited"


@pytest.mark.anyio
async def test_a_callback_without_the_browser_that_started_it_is_refused(site: Hosted):
    site.invite("ann@example.com")
    async with site.browser() as starter, site.browser() as attacker:
        first = await starter.get("/signin/oidc", params={"next": "/account"})
        state, code = site.idp.approve(first.headers["location"], "sub-ann", "ann@example.com")
        refused = await attacker.get("/oidc/callback", params={"state": state, "code": code})
        replayed = await starter.get("/oidc/callback", params={"state": state, "code": code})
    assert refused.status_code == 400
    assert replayed.status_code == 400, "the state is spent by the first attempt"
    assert site.idp.token_requests == 0


@pytest.mark.anyio
async def test_a_suspended_account_cannot_sign_in(site: Hosted):
    http = await site.member("ann@example.com")
    site.store.set_status("ann@example.com", "suspended")
    async with http:
        page = await http.get("/account")
    assert page.status_code == 403
    assert "suspended" in page.text


@pytest.mark.anyio
async def test_sign_in_is_rate_limited_per_address(site: Hosted):
    async with site.browser() as http:
        answers = [(await http.get("/signin/oidc")).status_code for _ in range(21)]
    assert answers[:20] == [302] * 20
    assert answers[20] == 429


# --- dynamic client registration ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_dcr_client_connects_and_reaches_its_own_vault(site: Hosted, vaults: SubjectVaults):
    http = await site.member("ann@example.com", sub="sub-ann")
    async with http:
        client = await register(http)
        tokens = await connect(site, http, client["client_id"])
        assert tokens["scope"] == "vault:read vault:write"
        assert tokens["refresh_token"]
        written = await call_tool(
            http, tokens["access_token"], "write_file", {"path": "Areas/a.md", "content": NOTE_BODY}
        )
    assert not written.get("isError"), text_of(written)
    assert "Hemligt" in vaults.for_subject(site.vault_of("ann@example.com")).backend.get("Areas/a.md")[0]


@pytest.mark.anyio
async def test_the_redirect_carries_state_and_the_issuer(site: Hosted):
    http = await site.member("ann@example.com")
    async with http:
        client = await register(http)
        _, started = await authorize(http, client["client_id"])
        _, approved = await consent(http, started.headers["location"])
    params = query(approved.headers["location"])
    assert params["state"] == "state-7"
    assert params["iss"] == BASE, MCP_AUTH


@pytest.mark.anyio
async def test_denying_returns_access_denied_to_the_client(site: Hosted):
    http = await site.member("ann@example.com")
    async with http:
        client = await register(http)
        _, started = await authorize(http, client["client_id"])
        _, denied = await consent(http, started.headers["location"], decision="deny")
    params = query(denied.headers["location"])
    assert params["error"] == "access_denied"
    assert params["state"] == "state-7"


@pytest.mark.anyio
async def test_a_redirect_uri_must_match_exactly(site: Hosted):
    async with site.browser() as http:
        client = await register(http, redirect="https://app.example.com/callback")
        _, refused = await authorize(http, client["client_id"], redirect="https://app.example.com/callback/../evil")
        _, other = await authorize(http, client["client_id"], redirect="https://app.example.com/other")
    assert refused.status_code == 400, MCP_SECURITY
    assert other.status_code == 400


@pytest.mark.anyio
async def test_a_loopback_redirect_matches_on_any_port(site: Hosted):
    async with site.browser() as http:
        client = await register(http, redirect="http://127.0.0.1/callback")
        _, any_port = await authorize(http, client["client_id"], redirect="http://127.0.0.1:53123/callback")
        _, other_path = await authorize(http, client["client_id"], redirect="http://127.0.0.1:53123/elsewhere")
    assert any_port.status_code == 302, CLAUDE_AUTH
    assert other_path.status_code == 400


@pytest.mark.anyio
async def test_registration_refuses_plain_http_redirects_off_loopback(site: Hosted):
    async with site.browser() as http:
        response = await http.post(
            "/register",
            json={
                "client_name": "x",
                "redirect_uris": ["http://evil.example.com/cb"],
                "token_endpoint_auth_method": "none",
            },
        )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


@pytest.mark.anyio
async def test_a_token_for_another_resource_is_refused(site: Hosted):
    http = await site.member("ann@example.com")
    async with http:
        client = await register(http)
        _, refused = await authorize(http, client["client_id"], resource="https://other.example.com/mcp")
    assert "invalid_target" in refused.headers.get("location", refused.text), MCP_SECURITY


@pytest.mark.anyio
async def test_a_refresh_token_rotates_and_works_once(site: Hosted):
    http = await site.member("ann@example.com")
    async with http:
        client = await register(http)
        tokens = await connect(site, http, client["client_id"])
        form = {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client["client_id"],
        }
        first = await http.post("/token", data=form)
        again = await http.post("/token", data=form)
    assert first.status_code == 200, first.text
    assert first.json()["refresh_token"] != tokens["refresh_token"], CLAUDE_AUTH
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"


@pytest.mark.anyio
async def test_suspension_stops_tokens_and_refreshes_at_once(site: Hosted):
    http = await site.member("ann@example.com")
    async with http:
        client = await register(http)
        tokens = await connect(site, http, client["client_id"])
        site.store.set_status("ann@example.com", "suspended")
        refused = await post_mcp(http, tokens["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        refresh = await http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"],
            },
        )
        site.store.set_status("ann@example.com", "active")
        restored = await post_mcp(http, tokens["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert refused.status_code == 403
    assert refresh.json()["error"] == "invalid_grant"
    assert restored.status_code != 403


@pytest.mark.anyio
async def test_dcr_clients_that_never_got_a_token_are_collected(site: Hosted):
    store = site.store
    async with site.browser() as http:
        idle = await register(http)
    used_member = await site.member("ann@example.com")
    async with used_member:
        used = await register(used_member)
        await connect(site, used_member, used["client_id"])
    with store.connect() as connection:
        connection.execute("UPDATE clients SET registered_at = registered_at - ?", (UNUSED_CLIENT_TTL + 60,))
    async with site.browser() as http:
        await register(http)
    assert store.client_document(idle["client_id"]) is None
    assert store.client_document(used["client_id"]) is not None


# --- client id metadata documents ---------------------------------------------------------------------------


def claude_document() -> dict[str, Any]:
    return {
        "client_id": CLAUDE_CODE_CIMD,
        "client_name": "Claude Code",
        "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


@pytest.mark.anyio
async def test_a_cimd_client_connects_as_a_public_client(site: Hosted):
    site.metadata.serve(CLAUDE_CODE_CIMD, claude_document())
    http = await site.member("ann@example.com")
    async with http:
        verifier, started = await authorize(http, CLAUDE_CODE_CIMD, redirect="http://localhost:3118/callback")
        assert started.status_code == 302, started.text
        page, approved = await consent(http, started.headers["location"])
        params = query(approved.headers["location"])
        token = await http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": params["code"],
                "redirect_uri": "http://localhost:3118/callback",
                "client_id": CLAUDE_CODE_CIMD,
                "code_verifier": verifier,
                "resource": f"{BASE}/mcp",
            },
        )
    assert "Claude Code" in page.text
    assert CLAUDE_CODE_CIMD in page.text, "the consent page names the document the client is identified by"
    assert "localhost" in page.text and "started it yourself" in page.text, MCP_SECURITY
    assert token.status_code == 200, token.text
    assert site.metadata.fetched.count(CLAUDE_CODE_CIMD) == 1, "the document is cached between authorize and token"


@pytest.mark.anyio
async def test_a_cimd_document_whose_client_id_differs_is_refused(site: Hosted):
    site.metadata.serve(CLAUDE_CODE_CIMD, {**claude_document(), "client_id": "https://claude.ai/other"})
    async with site.browser() as http:
        _, refused = await authorize(http, CLAUDE_CODE_CIMD, redirect="http://localhost:3118/callback")
    assert refused.status_code == 400, MCP_CIMD
    assert "not found" in refused.text


@pytest.mark.anyio
async def test_claude_ai_connects_with_its_hosted_callback(site: Hosted):
    url = "https://claude.ai/oauth/mcp-oauth-client-metadata"
    site.metadata.serve(url, {"client_id": url, "client_name": "Claude", "redirect_uris": [CLAUDE_CALLBACK]})
    http = await site.member("ann@example.com")
    async with http:
        _, started = await authorize(http, url, redirect=CLAUDE_CALLBACK)
        _, approved = await consent(http, started.headers["location"])
    assert approved.headers["location"].startswith(CLAUDE_CALLBACK + "?code="), CLAUDE_AUTH


# --- api keys -----------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_read_key_reads_and_cannot_write(site: Hosted):
    http = await site.member("ann@example.com")
    key = site.key_for("ann@example.com", write=False)
    async with http:
        listing = await call_tool(http, key, "list_files", {})
        written = await call_tool(http, key, "write_file", {"path": "Areas/a.md", "content": NOTE_BODY})
    assert key.startswith("nvm_")
    assert "Welcome.md" in text_of(listing)
    assert written.get("isError")
    assert "may only read" in text_of(written)


@pytest.mark.anyio
async def test_a_write_key_writes(site: Hosted):
    http = await site.member("ann@example.com")
    key = site.key_for("ann@example.com", write=True)
    async with http:
        written = await call_tool(http, key, "write_file", {"path": "Areas/a.md", "content": NOTE_BODY})
    assert not written.get("isError"), text_of(written)


@pytest.mark.anyio
async def test_a_revoked_key_is_refused(site: Hosted):
    http = await site.member("ann@example.com")
    key = site.key_for("ann@example.com")
    account = site.store.account("ann@example.com")
    site.store.revoke_key(account.id, key[4:16])
    async with http:
        refused = await post_mcp(http, key, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert refused.status_code == 401
    assert 'error="invalid_token"' in refused.headers["www-authenticate"]


@pytest.mark.anyio
async def test_a_key_with_a_wrong_secret_is_refused(site: Hosted):
    http = await site.member("ann@example.com")
    key = site.key_for("ann@example.com")
    forged = key[:16] + ("A" if key[16] != "A" else "B") + key[17:]
    async with http:
        refused = await post_mcp(http, forged, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert refused.status_code == 401


@pytest.mark.anyio
async def test_a_suspended_accounts_key_is_refused_at_once(site: Hosted):
    http = await site.member("ann@example.com")
    key = site.key_for("ann@example.com")
    site.store.set_status("ann@example.com", "suspended")
    async with http:
        refused = await post_mcp(http, key, INITIALIZE_BODY)
    assert refused.status_code == 403
    assert refused.json()["error"] == "account_suspended"


@pytest.mark.anyio
async def test_the_account_page_makes_lists_and_revokes_keys(site: Hosted):
    http = await site.member("ann@example.com")
    async with http:
        page = await http.get("/account/vaults/notes")
        made = await http.post(
            "/account/vaults/notes/keys", data={"csrf": csrf_of(page.text), "name": "devbox", "access": "write"}
        )
        key = made.text.split('<code id="new-key">')[1].split("<")[0]
        assert key.startswith("nvm_")
        listed = await http.get("/account/vaults/notes")
        assert "devbox" in listed.text and "read and write" in listed.text and key not in listed.text
        used = await call_tool(http, key, "list_files", {})
        assert not used.get("isError")
        key_id = key[4:16]
        forged = await http.post("/account/vaults/notes/keys/revoke", data={"csrf": "wrong", "key": key_id})
        assert forged.status_code == 403
        revoked = await http.post(
            "/account/vaults/notes/keys/revoke", data={"csrf": csrf_of(listed.text), "key": key_id}
        )
        assert revoked.status_code == 303
        refused = await post_mcp(http, key, INITIALIZE_BODY)
    assert refused.status_code == 401
    assert site.store.keys(site.store.account("ann@example.com").id) == []


@pytest.mark.anyio
async def test_a_key_works_as_the_webdav_password_and_a_read_key_cannot_put(site: Hosted):
    http = await site.member("ann@example.com")
    write = site.key_for("ann@example.com", write=True)
    read = site.key_for("ann@example.com", write=False, name="phone")
    async with http:
        put = await http.put("/dav/vault/Areas/w.md", content=b"# w\n", headers=basic(write))
        got = await http.get("/dav/vault/Areas/w.md", headers=basic(read))
        refused = await http.put("/dav/vault/Areas/r.md", content=b"# r\n", headers=basic(read))
    assert put.status_code in (201, 204), put.text
    assert got.content == b"# w\n"
    assert refused.status_code == 403


def basic(secret: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"ann@example.com:{secret}".encode()).decode()}


@pytest.mark.anyio
async def test_obsidian_access_mints_a_key_that_replaces_the_last(site: Hosted):
    http = await site.member("ann@example.com")
    key = site.key_for("ann@example.com")
    async with http:
        first = text_of(await call_tool(http, key, "obsidian_access", {}))
        second = text_of(await call_tool(http, key, "obsidian_access", {}))
    old = first.split("Password: ")[1].split()[0]
    new = second.split("Password: ")[1].split()[0]
    assert "Username: ann@example.com" in first
    account = site.store.account("ann@example.com")
    assert [row["name"] for row in site.store.keys(account.id)] == ["laptop", "Obsidian (WebDAV)"]
    assert site.store.key_grant(old) is None and site.store.key_grant(new) is not None


# --- isolation ----------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_two_accounts_never_see_each_others_notes_or_index(site: Hosted, vaults: SubjectVaults, tmp_path: Path):
    ann = await site.member("ann@example.com", sub="sub-ann")
    bo = await site.member("bo@example.com", sub="sub-bo")
    ann_key = site.key_for("ann@example.com")
    bo_key = site.key_for("bo@example.com")
    async with ann, bo:
        written = await call_tool(ann, ann_key, "write_file", {"path": "Areas/secret.md", "content": NOTE_BODY})
        assert not written.get("isError"), text_of(written)
        found = await call_tool(ann, ann_key, "search", {"query": "Hemligt"})
        hidden = await call_tool(bo, bo_key, "search", {"query": "Hemligt"})
        listing = await call_tool(bo, bo_key, "list_files", {})
        missing = await call_tool(bo, bo_key, "read_file", {"path": "Areas/secret.md"})
        dav = await bo.get("/dav/vault/Areas/secret.md", headers=basic(bo_key))
    assert "secret" in text_of(found)
    assert "secret" not in text_of(hidden)
    assert "Areas/secret.md" not in text_of(listing)
    assert missing.get("isError")
    assert dav.status_code == 404
    bo_files = {entry.key for entry in vaults.for_subject(site.vault_of("bo@example.com")).backend.list_all()}
    assert "Areas/secret.md" not in bo_files
    indexes = sorted(path.name for path in (tmp_path / "cache").glob("*.sqlite"))
    assert len(indexes) == 2, indexes


INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}
