from __future__ import annotations

import json
import os
import secrets
import signal
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx2 as httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from notes_vault_mcp.auth import AuthConfig
from notes_vault_mcp.auth import oidc as oidc_module
from notes_vault_mcp.auth.http import build_http_app
from notes_vault_mcp.auth.oidc import OidcVerifier
from notes_vault_mcp.backends.local import LocalBackend
from notes_vault_mcp.embedding import SharedEmbedder
from notes_vault_mcp.schema import load_schema
from notes_vault_mcp.vault import Vault

ISSUER = "https://idp.example.com"
JWKS_PATH = "/jwks"
USERINFO_PATH = "/userinfo"
PROTOCOL_VERSION = "2025-06-18"
NOTE = "---\ntitle: Oidc\ndate: 2026-08-01\nupdated: 2026-08-01\ntags: [mcp]\nstatus: active\n---\n\nKropp.\n"


@lru_cache(maxsize=1)
def _key_pool() -> tuple[Any, ...]:
    return tuple(rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(5))


AUDIENCE = "vault"


class Idp:
    def __init__(self, issuer: str = ISSUER) -> None:
        self.issuer = issuer
        self.spare = list(_key_pool())
        self.private: dict[str, Any] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        self.discovery_calls = 0
        self.jwks_calls = 0
        self.userinfo_calls = 0
        self.offline = False
        self.add_key("one")

    def add_key(self, kid: str) -> Any:
        key = self.spare.pop(0)
        self.private[kid] = key
        return key

    def token(self, kid: str = "one", userinfo: dict[str, Any] | None = None, **claims: Any) -> str:
        payload = {"iss": self.issuer, "sub": "ida", "aud": AUDIENCE, "exp": int(time.time()) + 300, **claims}
        encoded = jwt.encode(payload, self.private[kid], algorithm="RS256", headers={"kid": kid})
        if userinfo is not None:
            self.profiles[encoded] = userinfo
        return encoded

    def opaque_token(self, **profile: Any) -> str:
        value = f"pocket-id-{len(self.profiles)}-{secrets.token_hex(8)}"
        self.profiles[value] = profile
        return value

    def foreign_token(self, **claims: Any) -> str:
        payload = {"iss": self.issuer, "sub": "ida", "exp": int(time.time()) + 300, **claims}
        return jwt.encode(payload, self.spare[0], algorithm="RS256", headers={"kid": "stranger"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("no route to the identity provider")
        if request.url.path.endswith("/.well-known/openid-configuration"):
            self.discovery_calls += 1
            return httpx.Response(200, json=self.discovery())
        if request.url.path == JWKS_PATH:
            self.jwks_calls += 1
            return httpx.Response(200, json={"keys": [_jwk(kid, key) for kid, key in self.private.items()]})
        if request.url.path == USERINFO_PATH:
            self.userinfo_calls += 1
            return self.userinfo(request.headers.get("authorization", ""))
        return httpx.Response(404)

    def discovery(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "jwks_uri": self.issuer + JWKS_PATH,
            "userinfo_endpoint": self.issuer + USERINFO_PATH,
        }

    def userinfo(self, authorization: str) -> httpx.Response:
        profile = self.profiles.get(authorization.removeprefix("Bearer "))
        if profile is None:
            return httpx.Response(401, json={"error": "invalid_token"})
        return httpx.Response(200, json=profile)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def verifier(self, **overrides: Any) -> OidcVerifier:
        return OidcVerifier(self.config(**overrides), client=self.client())

    def config(self, **overrides: Any) -> AuthConfig:
        settings: dict[str, Any] = {
            "public_url": "https://vault.example.com",
            "issuer": self.issuer,
            "audience": AUDIENCE,
        }
        settings.update(overrides)
        return AuthConfig(mode="oidc", **settings)


def _jwk(kid: str, key: Any) -> dict[str, Any]:
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**public, "kid": kid, "alg": "RS256", "use": "sig"}


@pytest.fixture
def idp() -> Idp:
    return Idp()


@pytest.mark.anyio
async def test_a_token_in_the_write_group_may_read_and_write(idp: Idp):
    token = await idp.verifier().verify_token(idp.token(groups=["vault-writers"]))
    assert token is not None
    assert token.scopes == ["vault:read", "vault:write"]


@pytest.mark.anyio
async def test_a_token_in_the_read_group_may_only_read(idp: Idp):
    token = await idp.verifier().verify_token(idp.token(groups=["vault", "other"]))
    assert token is not None
    assert token.scopes == ["vault:read"]


@pytest.mark.anyio
async def test_a_token_in_neither_group_gets_no_access(idp: Idp):
    assert await idp.verifier().verify_token(idp.token(groups=["staff"])) is None


@pytest.mark.anyio
async def test_the_group_names_come_from_the_configuration(idp: Idp):
    verifier = idp.verifier(read_group="readers", write_group="writers")
    token = await verifier.verify_token(idp.token(groups=["writers"]))
    assert token is not None
    assert token.scopes == ["vault:read", "vault:write"]


@pytest.mark.anyio
async def test_a_scope_claim_grants_access_when_the_groups_do_not(idp: Idp):
    token = await idp.verifier().verify_token(idp.token(scope="openid profile vault:read"))
    assert token is not None
    assert token.scopes == ["vault:read"]


@pytest.mark.anyio
async def test_the_groups_from_userinfo_outrank_the_client_permissions(idp: Idp):
    raw = idp.token(scope="openid vault:read vault:write", userinfo={"sub": "anna", "groups": ["vault"]})
    token = await idp.verifier().verify_token(raw)
    assert token is not None
    assert token.scopes == ["vault:read"]


@pytest.mark.anyio
async def test_the_groups_in_the_token_outrank_the_client_permissions(idp: Idp):
    token = await idp.verifier().verify_token(idp.token(scope="openid vault:read vault:write", groups=["vault"]))
    assert token is not None
    assert token.scopes == ["vault:read"]


@pytest.mark.anyio
async def test_a_token_from_another_issuer_is_refused(idp: Idp):
    assert await idp.verifier().verify_token(idp.token(iss="https://evil.example.com", groups=["vault"])) is None


@pytest.mark.anyio
async def test_an_expired_token_is_refused(idp: Idp):
    expired = idp.token(groups=["vault"], exp=int(time.time()) - 60)
    assert await idp.verifier().verify_token(expired) is None


@pytest.mark.anyio
async def test_a_token_for_another_audience_is_refused_when_an_audience_is_configured(idp: Idp):
    verifier = idp.verifier(audience="vault")
    assert await verifier.verify_token(idp.token(groups=["vault"], aud="grafana")) is None


@pytest.mark.anyio
async def test_the_configured_audience_is_accepted(idp: Idp):
    verifier = idp.verifier(audience="vault")
    token = await verifier.verify_token(idp.token(groups=["vault"], aud="vault"))
    assert token is not None
    assert token.client_id == "vault"


@pytest.mark.anyio
async def test_no_token_is_accepted_when_no_audience_is_configured(idp: Idp):
    verifier = idp.verifier(audience="")
    assert await verifier.verify_token(idp.token(groups=["vault"], aud="grafana")) is None
    assert await verifier.verify_token(idp.token(groups=["vault"])) is None


@pytest.mark.anyio
async def test_a_token_without_an_audience_is_refused(idp: Idp):
    assert await idp.verifier().verify_token(idp.token(groups=["vault"], aud=None)) is None


@pytest.mark.anyio
async def test_the_access_token_carries_the_subject_resource_and_claims(idp: Idp):
    verifier = idp.verifier(audience="pocket-id-client")
    token = await verifier.verify_token(idp.token(groups=["vault"], azp="pocket-id-client", aud="pocket-id-client"))
    assert token is not None
    assert token.subject == "ida"
    assert token.resource == "https://vault.example.com/mcp"
    assert token.client_id == "pocket-id-client"
    assert token.claims is not None and token.claims["groups"] == ["vault"]


@pytest.mark.anyio
async def test_a_token_signed_by_an_unpublished_key_is_refused(idp: Idp):
    assert await idp.verifier().verify_token(idp.foreign_token(groups=["vault"])) is None


@pytest.mark.anyio
async def test_a_token_that_is_not_a_jwt_is_refused(idp: Idp):
    assert await idp.verifier().verify_token("nonsense") is None


@pytest.mark.anyio
async def test_a_provider_that_cannot_be_reached_refuses_the_token(idp: Idp):
    verifier = idp.verifier()
    idp.offline = True
    assert await verifier.verify_token(idp.token(groups=["vault"])) is None


@pytest.mark.anyio
async def test_the_keys_are_cached_across_tokens(idp: Idp):
    verifier = idp.verifier()
    await verifier.verify_token(idp.token(groups=["vault"]))
    await verifier.verify_token(idp.token(groups=["vault"]))
    assert (idp.discovery_calls, idp.jwks_calls) == (1, 1)


@pytest.mark.anyio
async def test_an_unknown_kid_refetches_the_jwks_at_most_once_a_minute(idp: Idp):
    verifier = idp.verifier()
    await verifier.verify_token(idp.token(groups=["vault"]))
    idp.add_key("two")
    rotated = await verifier.verify_token(idp.token("two", groups=["vault"]))
    assert rotated is not None
    assert idp.jwks_calls == 2
    idp.add_key("three")
    assert await verifier.verify_token(idp.token("three", groups=["vault"])) is None
    assert idp.jwks_calls == 2


@pytest.mark.anyio
async def test_a_jwt_without_groups_takes_them_from_userinfo(idp: Idp):
    token = idp.token(userinfo={"sub": "ida", "groups": ["vault-writers"]})
    verified = await idp.verifier().verify_token(token)
    assert verified is not None
    assert verified.scopes == ["vault:read", "vault:write"]
    assert idp.userinfo_calls == 1


@pytest.mark.anyio
async def test_a_token_that_names_its_groups_never_asks_userinfo(idp: Idp):
    await idp.verifier().verify_token(idp.token(groups=["vault"]))
    assert idp.userinfo_calls == 0


@pytest.mark.anyio
async def test_the_userinfo_answer_is_cached_per_token(idp: Idp):
    verifier = idp.verifier()
    token = idp.token(userinfo={"sub": "ida", "groups": ["vault"]})
    await verifier.verify_token(token)
    await verifier.verify_token(token)
    assert idp.userinfo_calls == 1
    await verifier.verify_token(idp.token(sub="ada", userinfo={"sub": "ada", "groups": ["vault"]}))
    assert idp.userinfo_calls == 2


@pytest.mark.anyio
async def test_userinfo_without_a_known_group_refuses_the_token(idp: Idp):
    token = idp.token(userinfo={"sub": "ida", "groups": ["staff"]})
    assert await idp.verifier().verify_token(token) is None


@pytest.mark.anyio
async def test_a_userinfo_call_the_provider_rejects_refuses_the_token(idp: Idp):
    assert await idp.verifier().verify_token(idp.token()) is None
    assert idp.userinfo_calls == 1


@pytest.mark.anyio
async def test_an_opaque_token_is_accepted_through_userinfo(idp: Idp):
    verified = await idp.verifier().verify_token(idp.opaque_token(sub="ida", groups=["vault"]))
    assert verified is not None
    assert verified.scopes == ["vault:read"]
    assert verified.subject == "ida"
    assert verified.expires_at is None
    assert verified.resource == "https://vault.example.com/mcp"


@pytest.mark.anyio
async def test_an_opaque_token_without_a_subject_is_refused(idp: Idp):
    assert await idp.verifier().verify_token(idp.opaque_token(groups=["vault"])) is None


@pytest.mark.anyio
async def test_an_opaque_token_the_provider_does_not_know_is_refused(idp: Idp):
    assert await idp.verifier().verify_token("pocket-id-stranger") is None


def _request(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def payload(response: httpx.Response) -> dict[str, Any]:
    if response.headers["content-type"].startswith("application/json"):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    raise AssertionError(f"no JSON-RPC payload in {response.text!r}")


class Session:
    def __init__(self, client: httpx.AsyncClient, token: str, path: str = "/mcp") -> None:
        self.client = client
        self.path = path
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    async def post(self, body: dict[str, Any]) -> httpx.Response:
        return await self.client.post(self.path, json=body, headers=self.headers)

    async def initialize(self) -> dict[str, Any]:
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "tests", "version": "0"},
        }
        response = await self.post(_request(1, "initialize", params))
        assert response.status_code == 200, response.text
        self.headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        await self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return payload(response)

    async def call(self, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await self.post(_request(request_id, method, params))
        assert response.status_code == 200, response.text
        return payload(response)

    async def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        answer = await self.call(2, "tools/call", {"name": name, "arguments": arguments})
        return answer["result"]


def oidc_app(
    vault: Vault,
    idp: Idp,
    monkeypatch: pytest.MonkeyPatch,
    public_url: str = "http://localhost",
    **overrides: Any,
) -> Any:
    monkeypatch.setattr(
        oidc_module, "OidcVerifier", lambda config: OidcVerifier(config, client=idp.client()), raising=True
    )
    return build_http_app(vault, idp.config(public_url=public_url, **overrides))


@asynccontextmanager
async def session(app: Any, token: str, path: str = "/mcp") -> AsyncIterator[Session]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8765") as client:
            yield Session(client, token, path)


def text_of(result: dict[str, Any]) -> str:
    return "".join(block.get("text", "") for block in result.get("content", []))


@pytest.mark.anyio
async def test_the_mcp_endpoint_refuses_a_request_without_a_token(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch)
    async with session(app, "") as opened:
        response = await opened.post(_request(1, "initialize", {}))
    assert response.status_code == 401
    assert (
        'resource_metadata="http://localhost/.well-known/oauth-protected-resource/mcp"'
        in (response.headers["www-authenticate"])
    )


@pytest.mark.anyio
async def test_a_read_token_can_initialize_and_list_the_tools(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch)
    async with session(app, idp.token(groups=["vault"])) as opened:
        handshake = await opened.initialize()
        listing = await opened.call(2, "tools/list", {})
    assert handshake["result"]["serverInfo"]["name"] == "vault"
    assert "write_file" in {tool["name"] for tool in listing["result"]["tools"]}


@pytest.mark.anyio
async def test_a_read_only_token_may_not_write(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch)
    async with session(app, idp.token(groups=["vault"])) as opened:
        await opened.initialize()
        result = await opened.tool("write_file", {"path": "Areas/oidc.md", "content": NOTE})
    assert result["isError"] is True
    assert "vault:write" in text_of(result)


@pytest.mark.anyio
async def test_a_write_token_may_write(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch)
    async with session(app, idp.token(groups=["vault-writers"])) as opened:
        await opened.initialize()
        result = await opened.tool("write_file", {"path": "Areas/oidc.md", "content": NOTE})
    assert not result.get("isError")
    assert "Written: Areas/oidc.md" in text_of(result)


@pytest.mark.anyio
async def test_the_metadata_advertises_the_scopes_the_provider_knows(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch)
    async with session(app, "") as opened:
        metadata = (await opened.client.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert metadata["scopes_supported"] == ["openid", "profile", "email", "groups"]
    assert metadata["resource"] == "http://localhost/mcp"
    assert metadata["authorization_servers"] == [ISSUER]


@pytest.mark.anyio
async def test_the_advertised_scopes_come_from_the_configuration(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch, idp_scopes=("openid", "groups"))
    async with session(app, "") as opened:
        metadata = (await opened.client.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert metadata["scopes_supported"] == ["openid", "groups"]


@pytest.mark.anyio
async def test_a_token_the_provider_will_not_describe_is_refused_by_the_endpoint(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch)
    async with session(app, idp.token()) as opened:
        response = await opened.post(_request(1, "initialize", {}))
    assert response.status_code == 401
    assert idp.userinfo_calls == 1


@pytest.mark.anyio
async def test_an_opaque_token_reaches_the_tools(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch)
    async with session(app, idp.opaque_token(sub="ida", groups=["vault"])) as opened:
        await opened.initialize()
        listing = await opened.call(2, "tools/list", {})
    assert "search" in {tool["name"] for tool in listing["result"]["tools"]}


SEMANTIC_NOTE = "---\ntitle: {title}\ndate: 2026-10-01\nupdated: 2026-10-01\ntags: []\nstatus: active\n---\n{body}\n"


@pytest.mark.anyio
async def test_search_finds_by_meaning_while_the_server_embeds_a_changed_note(tmp_path, idp: Idp, monkeypatch):
    tests = str(Path(__file__).parent)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([tests, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("FAKE_EMBED_DELAY", "3")
    monkeypatch.setenv("FAKE_EMBED_QUICK", "inloggning")
    root = tmp_path / "vault"
    (root / "Areas").mkdir(parents=True)
    (root / "Areas/auth.md").write_text(SEMANTIC_NOTE.format(title="Anslutning", body="oauth token"), encoding="utf-8")
    log = root / "Areas/log.md"
    log.write_text(SEMANTIC_NOTE.format(title="Logg", body="deploy"), encoding="utf-8")
    cache = tmp_path / "cache"
    embedder = SharedEmbedder(cache, "fake-1", loader="fake_models:concept_model", query_wait=1.0)
    backend = LocalBackend(root)
    vault = Vault(backend, load_schema(backend), cache / "index.sqlite", embedder=embedder, background=True)
    try:
        vault.index.sync(force=True)
        vault.index.vectors.wait(30)
        log.write_text(SEMANTIC_NOTE.format(title="Logg", body="deploy release"), encoding="utf-8")
        vault.index.set_meta("last_sync", "0")
        app = oidc_app(vault, idp, monkeypatch)
        async with session(app, idp.token(groups=["vault"])) as opened:
            await opened.initialize()
            await opened.tool("search", {"query": "inloggning"})
            time.sleep(0.3)
            during = text_of(await opened.tool("search", {"query": "inloggning"}))
        assert vault.index.vectors._worker.is_alive()
        assert during.startswith("1 of 1, 1 by meaning only") and "Areas/auth.md" in during
    finally:
        vault.close()
        os.kill(int((cache / "embed.pid").read_text()), signal.SIGTERM)
