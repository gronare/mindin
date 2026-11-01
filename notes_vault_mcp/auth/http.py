from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from notes_vault_mcp.auth import READ_SCOPE, SCOPES, AuthConfig, auth_dir
from notes_vault_mcp.vault import Vault, VaultResolver


def auth_settings(auth: AuthConfig) -> AuthSettings:
    builtin = auth.mode == "builtin"
    return AuthSettings(
        issuer_url=auth.issuer,
        resource_server_url=auth.resource_url,
        validate_token_resource=False,
        required_scopes=[READ_SCOPE] if builtin else None,
        client_registration_options=ClientRegistrationOptions(
            enabled=builtin, valid_scopes=list(SCOPES), default_scopes=[READ_SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=builtin),
    )


def server_auth_kwargs(auth: AuthConfig | None) -> dict[str, Any]:
    if auth is None or auth.mode in ("bearer", "forwarded", "hosted"):
        return {}
    if auth.mode == "oidc":
        from notes_vault_mcp.auth.oidc import OidcVerifier

        return {"token_verifier": OidcVerifier(auth), "auth": auth_settings(auth)}
    from notes_vault_mcp.auth.builtin import BuiltinProvider

    return {"auth_server_provider": BuiltinProvider(auth), "auth": auth_settings(auth)}


class BearerToken:
    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"unauthorized"})

    def _authorized(self, scope: dict) -> bool:
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                return secrets.compare_digest(value, f"Bearer {self.token}".encode())
        return False


LOCAL_HOSTS = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"]
LOCAL_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def transport_security(auth: AuthConfig) -> TransportSecuritySettings | None:
    hosts: list[str] = []
    origins: list[str] = []
    if auth.public_host:
        hosts += [auth.public_host, f"{auth.public_host}:*"]
    if auth.public_url and "://" in auth.public_url:
        parts = urlsplit(auth.public_url)
        origins.append(f"{parts.scheme}://{parts.netloc}")
    for name in auth.allowed_hosts:
        hosts += [name, f"{name}:*"]
        origins += [f"https://{name}", f"https://{name}:*", f"http://{name}", f"http://{name}:*"]
    if not hosts:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*hosts, *LOCAL_HOSTS],
        allowed_origins=[*origins, *LOCAL_ORIGINS],
    )


def build_http_app(vaults: Vault | VaultResolver, auth: AuthConfig, host: str = "127.0.0.1") -> Any:
    from notes_vault_mcp.server import build_server

    if auth.mode == "forwarded":
        return forwarded_app(vaults, auth, host)
    if auth.mode == "hosted":
        from notes_vault_mcp.auth.hosted import build_hosted_app

        return build_hosted_app(vaults, auth, host)
    server: MCPServer = build_server(vaults, auth)
    if auth.mode == "builtin":
        provider = server._auth_server_provider
        provider.register_routes(server)
    app = server.streamable_http_app(host=host, transport_security=transport_security(auth))
    if auth.mode == "bearer":
        return BearerToken(app, auth.bearer_token)
    from notes_vault_mcp.auth.prefix import mount_under_prefix

    return mount_under_prefix(app, auth, server)


def forwarded_app(vaults: Vault | VaultResolver, auth: AuthConfig, host: str) -> Any:
    # Behind a proxy that authenticates for us: identity comes from a signed header, the transport is stateless
    # so the proxy's buffered relay needs no session id, and the Host header is whatever the proxy sends.
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.routing import Mount

    from notes_vault_mcp.auth.forwarded import ForwardedIdentity, ForwardedVerifier
    from notes_vault_mcp.auth.personal import PersonalTokens
    from notes_vault_mcp.auth.prefix import forwarded_lifespan
    from notes_vault_mcp.dav import WebDav
    from notes_vault_mcp.server import build_server
    from notes_vault_mcp.vault import SubjectVaults

    tokens = PersonalTokens(auth.auth_dir or auth_dir())
    server = build_server(vaults, auth, personal_tokens=tokens, dav_url=auth.dav_url)
    mcp_app = server.streamable_http_app(
        host=host,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    prefix = auth.path_prefix
    routes = []
    if isinstance(vaults, SubjectVaults):
        routes.append(Mount(prefix + "/dav", app=WebDav(vaults, tokens, mount=prefix + "/dav")))
    routes.append(Mount(prefix or "/", app=mcp_app))
    guard = Middleware(
        ForwardedIdentity,
        verifier=ForwardedVerifier(auth),
        header=auth.identity_header,
        health_path=prefix + "/up",
        open_prefixes=(prefix + "/dav",),
    )
    return Starlette(routes=routes, middleware=[guard], lifespan=forwarded_lifespan(mcp_app))
