from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import jwt
from cimd_support import MetadataHost
from forwarded_support import _keys
from login_support import GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, FakeGithub, FakeSmtp

from mindin.auth import AuthConfig, GithubConfig, OidcConfig, SmtpConfig
from mindin.auth.hosted import build_hosted_app
from mindin.auth.hosted_keys import Keyring, MasterKey
from mindin.auth.hosted_limits import StorageLimits
from mindin.auth.hosted_store import Account, HostedStore
from mindin.auth.identities import Identities
from mindin.extensions import Extension

BASE = "http://localhost"
IDP = "https://id.example.test"
CLIENT_ID = "vault-server"
CLIENT_SECRET = "upstream-secret-value"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}
NOTE_BODY = (
    "---\ntitle: Hemligt\ndate: 2026-09-01\nupdated: 2026-09-01\ntags: [work]\nstatus: active\n---\n\n"
    "Hemligt innehåll.\n"
)
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "hosted", "version": "1"}},
}


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


@dataclass
class FakeIdentityProvider:
    issuer: str = IDP
    grants: dict[str, dict[str, Any]] = field(default_factory=dict)
    token_requests: int = 0

    @property
    def key(self) -> Any:
        return _keys()[0]

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx2.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{self.issuer}/authorize",
                    "token_endpoint": f"{self.issuer}/api/oidc/token",
                    "jwks_uri": f"{self.issuer}/.well-known/jwks.json",
                },
            )
        if path == "/.well-known/jwks.json":
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx2.Response(200, json={"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]})
        if path == "/api/oidc/token":
            return self.token(request)
        return httpx2.Response(404)

    def approve(self, authorize_url: str, sub: str, email: str, verified: bool = True) -> tuple[str, str]:
        params = query(authorize_url)
        assert authorize_url.startswith(f"{self.issuer}/authorize?")
        assert params["client_id"] == CLIENT_ID
        assert params["code_challenge_method"] == "S256"
        assert params["redirect_uri"] == f"{BASE}/oidc/callback"
        code = secrets.token_urlsafe(16)
        self.grants[code] = {**params, "sub": sub, "email": email, "verified": verified}
        return params["state"], code

    def token(self, request: httpx2.Request) -> httpx2.Response:
        self.token_requests += 1
        expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if request.headers.get("authorization") != f"Basic {expected}":
            return httpx2.Response(401, json={"error": "invalid_client"})
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        grant = self.grants.pop(form.get("code", ""), None)
        if grant is None:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        if base64.urlsafe_b64encode(digest).decode().rstrip("=") != grant["code_challenge"]:
            return httpx2.Response(400, json={"error": "invalid_grant", "error_description": "pkce"})
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "sub": grant["sub"],
            "email": grant["email"],
            "email_verified": grant["verified"],
            "name": grant["email"].split("@")[0],
            "nonce": grant["nonce"],
            "iat": now,
            "exp": now + 300,
        }
        id_token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})
        return httpx2.Response(200, json={"access_token": "upstream", "token_type": "Bearer", "id_token": id_token})


def hosted_config(tmp_path: Path, **overrides: Any) -> AuthConfig:
    settings: dict[str, Any] = {
        "public_url": BASE,
        "issuer": BASE,
        "auth_dir": tmp_path / "auth",
        "oidc": OidcConfig(issuer=IDP, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, name="Pocket ID"),
        "smtp": SmtpConfig(
            host="smtp.example.test", sender="vault@example.test", username="mailer", password="smtp-password"
        ),
        "github": GithubConfig(client_id=GITHUB_CLIENT_ID, client_secret=GITHUB_CLIENT_SECRET),
    }
    settings.update(overrides)
    return AuthConfig(**settings)


class FixedLimits(Extension):
    def __init__(self, limits: StorageLimits) -> None:
        self.fixed = limits

    def storage_limits(self, account: Account, limits: StorageLimits) -> StorageLimits:
        return self.fixed


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Browser(httpx2.AsyncClient):
    async def __aenter__(self) -> Browser:
        return self if self._state.name == "OPENED" else await super().__aenter__()


@dataclass
class Hosted:
    app: Any
    config: AuthConfig
    idp: FakeIdentityProvider
    metadata: MetadataHost
    clock: Clock
    github: FakeGithub
    smtp: FakeSmtp
    keyring: Keyring

    @property
    def store(self) -> HostedStore:
        return HostedStore(self.config.auth_dir)

    def browser(self) -> httpx2.AsyncClient:
        return Browser(transport=httpx2.ASGITransport(app=self.app), base_url=BASE)

    async def sign_in(
        self, http: httpx2.AsyncClient, email: str, sub: str = "", path: str = "/account", verified: bool = True
    ) -> httpx2.Response:
        first = await http.get(path)
        assert first.status_code == 303, first.text
        assert first.headers["location"].startswith("/signin?next="), first.headers["location"]
        start = await http.get("/signin/oidc", params={"next": query(first.headers["location"])["next"]})
        assert start.status_code == 302, start.text
        state, code = self.idp.approve(start.headers["location"], sub or f"sub-{email.split('@')[0]}", email, verified)
        return await http.get("/oidc/callback", params={"state": state, "code": code})

    @property
    def identities(self) -> Identities:
        return Identities(self.store)

    def sub_of(self, email: str) -> str:
        account = self.store.account(email)
        assert account is not None and account.sub
        return account.sub

    def vault_of(self, email: str, slug: str | None = None) -> str:
        account = self.store.account(email)
        assert account is not None
        vaults = self.store.vaults(account.id)
        found = [vault for vault in vaults if slug is None or vault.slug == slug]
        assert found, f"{email} has no vault {slug}"
        return found[0].id

    def invite(self, email: str) -> None:
        self.store.invite(email)

    async def member(self, email: str, sub: str = "") -> httpx2.AsyncClient:
        self.invite(email)
        http = self.browser()
        signed_in = await self.sign_in(http, email, sub)
        assert signed_in.status_code == 303, signed_in.text
        return http

    def key_for(self, email: str, write: bool = True, name: str = "laptop", vault: str | None = None) -> str:
        account = self.store.account(email)
        assert account is not None
        scopes = ["vault:read", "vault:write"] if write else ["vault:read"]
        return self.store.create_key(account.id, name, scopes, vault_id=self.vault_of(email, vault))


@asynccontextmanager
async def hosted(
    vaults: Any, tmp_path: Path, keyring: Keyring | None = None, extensions: Any = (), **overrides: Any
) -> AsyncIterator[Hosted]:
    idp = FakeIdentityProvider()
    metadata = MetadataHost()
    clock = Clock()
    github = FakeGithub()
    smtp = FakeSmtp()
    keyring = keyring or Keyring(MasterKey.generate())
    config = hosted_config(tmp_path, **overrides)
    app = build_hosted_app(
        vaults,
        config,
        oidc_transport=idp.transport(),
        github_transport=github.transport(),
        smtp_factory=smtp.factory,
        metadata_transport=metadata.transport(),
        resolver=metadata.resolve,
        clock=clock,
        keyring=keyring,
        extensions=extensions,
    )
    async with app.router.lifespan_context(app):
        yield Hosted(
            app=app,
            config=config,
            idp=idp,
            metadata=metadata,
            clock=clock,
            github=github,
            smtp=smtp,
            keyring=keyring,
        )


def key_for_subject(auth_dir: Path, email: str, sub: str, write: bool = True) -> str:
    store = HostedStore(auth_dir)
    account, _ = store.invite(email)
    with store.connect() as connection:
        connection.execute("UPDATE accounts SET status = 'active', sub = ? WHERE id = ?", (sub, account.id))
    return store.create_key(account.id, "dav", ["vault:read", "vault:write"] if write else ["vault:read"])


def first_vault_id(auth_dir: Path, email: str) -> str:
    store = HostedStore(auth_dir)
    account = store.account(email)
    assert account is not None
    vault = store.first_vault(account.id)
    assert vault is not None
    return vault.id


def vault_choice_of(page: str) -> str:
    marker = 'name="vault" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]


def csrf_of(page: str) -> str:
    marker = 'name="csrf" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]


async def post_mcp(
    http: httpx2.AsyncClient, token: str, body: dict[str, Any], session: str | None = None, path: str = "/mcp"
) -> httpx2.Response:
    headers = dict(MCP_HEADERS, Authorization=f"Bearer {token}")
    if session is not None:
        headers["mcp-session-id"] = session
    return await http.post(path, headers=headers, content=json.dumps(body))


def sse_payload(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(text)


async def open_session(http: httpx2.AsyncClient, token: str, path: str = "/mcp") -> str:
    response = await post_mcp(http, token, INITIALIZE, path=path)
    assert response.status_code == 200, response.text
    session = response.headers["mcp-session-id"]
    ready = await post_mcp(http, token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session, path)
    assert ready.status_code == 202, ready.text
    return session


async def call_tool(
    http: httpx2.AsyncClient, token: str, name: str, arguments: dict[str, Any], path: str = "/mcp"
) -> dict[str, Any]:
    session = await open_session(http, token, path)
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    response = await post_mcp(http, token, body, session, path)
    assert response.status_code == 200, response.text
    return sse_payload(response.text)["result"]


def text_of(result: dict[str, Any]) -> str:
    return "".join(block.get("text", "") for block in result.get("content", []))
