from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.routes import build_resource_metadata_url, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import BaseRoute, Mount, Route, request_response
from starlette.types import ASGIApp, Receive, Scope, Send

from notes_vault_mcp.auth import READ_SCOPE, SCOPES, AuthConfig, auth_dir
from notes_vault_mcp.auth.cimd import (
    ClientMetadataError,
    ClientMetadataFetcher,
    HostedClient,
    Resolver,
    allowed_redirect,
    is_metadata_url,
)
from notes_vault_mcp.auth.hosted_store import Account, ApiKeyCredentials, HostedStore
from notes_vault_mcp.auth.hosted_web import Browser, Limits, oauth_error
from notes_vault_mcp.auth.prefix import URL, forwarded_lifespan
from notes_vault_mcp.auth.upstream import Upstream

logger = logging.getLogger(__name__)

CODE_TTL = 300
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
REQUEST_TTL = 900
FORM_BODY_LIMIT = 64 * 1024
AUTHORIZATION_SERVER_PATH = "/.well-known/oauth-authorization-server"


def canonical(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


@dataclass(frozen=True)
class Verdict:
    access: AccessToken | None
    status: int = 200
    error: str = ""
    description: str = ""


class HostedProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, config: AuthConfig, store: HostedStore, fetcher: ClientMetadataFetcher) -> None:
        self.config = config
        self.store = store
        self.fetcher = fetcher

    def for_this_resource(self, resource: str | None) -> bool:
        return resource is None or canonical(resource) == canonical(self.config.resource_url)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if is_metadata_url(client_id):
            try:
                return await self.fetcher.client(client_id)
            except ClientMetadataError as exc:
                logger.warning("client metadata document refused: %s", exc)
                return None
        document = self.store.client_document(client_id)
        return HostedClient.model_validate_json(document) if document else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        redirects = [str(uri) for uri in client_info.redirect_uris or []]
        if not redirects or not all(allowed_redirect(uri) for uri in redirects):
            raise RegistrationError("invalid_redirect_uri", "redirect URIs must be https, loopback http or app schemes")
        self.store.save_client(
            client_info.client_id,
            client_info.client_name or "unnamed client",
            client_info.model_dump_json(exclude_none=True),
        )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self.for_this_resource(params.resource):
            raise AuthorizeError("invalid_target", f"this server only issues tokens for {self.config.resource_url}")
        request_id = secrets.token_urlsafe(24)
        self.store.save_request(
            request_id,
            {
                "client_id": client.client_id,
                "client_name": client.client_name or client.client_id,
                "state": params.state,
                "scopes": params.scopes or [READ_SCOPE],
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            },
            REQUEST_TTL,
        )
        return f"{self.config.public_url.rstrip('/')}/login?request={request_id}"

    def mint_code(self, pending: dict[str, Any], scopes: list[str], account: Account) -> str:
        code = secrets.token_urlsafe(32)
        record = {
            "code": code,
            "client_id": pending["client_id"],
            "scopes": scopes,
            "expires_at": time.time() + CODE_TTL,
            "code_challenge": pending["code_challenge"],
            "redirect_uri": pending["redirect_uri"],
            "redirect_uri_provided_explicitly": pending["redirect_uri_provided_explicitly"],
            "resource": self.config.resource_url,
            "subject": account.sub,
            "account_id": account.id,
        }
        self.store.save_code(code, record["expires_at"], record)
        return code

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        record = self.store.load_code(authorization_code)
        if record is None or record["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate(record)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        record = self.store.load_code(authorization_code.code)
        if record is None or not self.store.take_code(authorization_code.code):
            raise TokenError("invalid_grant", "authorization code does not exist")
        if record["expires_at"] < time.time():
            raise TokenError("invalid_grant", "authorization code has expired")
        self.require_active(record["account_id"])
        return self.issue(client.client_id, record["account_id"], authorization_code.scopes)

    def require_active(self, account_id: str) -> Account:
        account = self.store.account(account_id)
        if account is None or not account.active:
            raise TokenError("invalid_grant", "the account is not active")
        return account

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self.store.load_token(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject=row["account_id"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        if not self.store.revoke_token(refresh_token.token):
            raise TokenError("invalid_grant", "refresh token does not exist")
        account = self.require_active(refresh_token.subject or "")
        return self.issue(client.client_id, account.id, scopes or refresh_token.scopes)

    def issue(self, client_id: str, account_id: str, scopes: list[str]) -> OAuthToken:
        grant = {
            "grant_id": secrets.token_urlsafe(16),
            "client_id": client_id,
            "account_id": account_id,
            "scopes": scopes,
            "resource": self.config.resource_url,
        }
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.store.save_token(access, "access", grant, ACCESS_TTL)
        self.store.save_token(refresh, "refresh", grant, REFRESH_TTL)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self.authenticate(token).access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revoke_grant(token.token)

    def authenticate(self, token: str) -> Verdict:
        grant = self.store.key_grant(token)
        if grant is not None:
            return self.verdict(grant.account, token, f"key:{grant.key_id}", grant.scopes, None)
        row = self.store.load_token(token, "access")
        if row is None or row["expires_at"] <= time.time():
            return Verdict(None, 401, "invalid_token", "the token is unknown, revoked or expired")
        if canonical(row["resource"]) != canonical(self.config.resource_url):
            return Verdict(None, 401, "invalid_token", "the token was issued for another resource")
        account = self.store.account(row["account_id"])
        if account is None:
            return Verdict(None, 401, "invalid_token", "the token's account is gone")
        return self.verdict(account, token, row["client_id"], row["scopes"].split(), int(row["expires_at"]))

    def verdict(
        self, account: Account, token: str, client_id: str, scopes: list[str], expires_at: int | None
    ) -> Verdict:
        if not account.active or not account.sub:
            return Verdict(None, 403, "account_suspended", "the account is not active")
        access = AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at,
            resource=self.config.resource_url,
            subject=account.sub,
            claims={"email": account.email, "account": account.id},
        )
        return Verdict(access)


class HostedGuard:
    def __init__(self, app: ASGIApp, provider: HostedProvider, protected: str, metadata_url: str) -> None:
        self.app = app
        self.provider = provider
        self.protected = protected
        self.metadata_url = metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == self.protected or path.startswith(self.protected + "/")):
            await self.app(scope, receive, send)
            return
        token = bearer(scope)
        verdict = self.provider.authenticate(token) if token else Verdict(None, 401)
        if verdict.access is None:
            await self.refuse(send, verdict)
            return
        user = AuthenticatedUser(verdict.access)
        context = auth_context_var.set(user)
        try:
            await self.app({**scope, "user": user, "auth": AuthCredentials(verdict.access.scopes)}, receive, send)
        finally:
            auth_context_var.reset(context)

    async def refuse(self, send: Send, verdict: Verdict) -> None:
        challenge = f'Bearer resource_metadata="{self.metadata_url}", scope="{READ_SCOPE}"'
        if verdict.error == "invalid_token":
            challenge += f', error="invalid_token", error_description="{verdict.description}"'
        body = json.dumps({"error": verdict.error or "invalid_token", "error_description": verdict.description})
        headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]
        if verdict.status == 401:
            headers.append((b"www-authenticate", challenge.encode("latin-1")))
        await send({"type": "http.response.start", "status": verdict.status, "headers": headers})
        await send({"type": "http.response.body", "body": body.encode("utf-8")})


def bearer(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            return token.strip() if scheme.lower() == "bearer" else ""
    return ""


def metadata(config: AuthConfig) -> dict[str, Any]:
    base = config.public_url.rstrip("/")
    client_methods = ["client_secret_post", "client_secret_basic", "none"]
    return {
        "issuer": config.issuer,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "revocation_endpoint": f"{base}/revoke",
        "scopes_supported": list(SCOPES),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": client_methods,
        "revocation_endpoint_auth_methods_supported": client_methods,
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    }


def cors(app: ASGIApp, methods: list[str]) -> ASGIApp:
    return CORSMiddleware(
        app=app, allow_origins="*", allow_methods=methods, allow_headers=[MCP_PROTOCOL_VERSION_HEADER]
    )


def limited(handler: Callable[[Request], Any]) -> ASGIApp:
    return RequestBodyLimitMiddleware(request_response(handler), FORM_BODY_LIMIT)


class TokenEndpoint:
    def __init__(self, provider: HostedProvider) -> None:
        self.provider = provider
        self.handler = TokenHandler(provider, ClientAuthenticator(provider))

    async def handle(self, request: Request) -> Response:
        form = await request.form()
        resource = form.get("resource")
        if isinstance(resource, str) and not self.provider.for_this_resource(resource):
            return oauth_error(
                "invalid_target", f"this server only issues tokens for {self.provider.config.resource_url}"
            )
        return await self.handler.handle(request)


def build_hosted_app(
    vaults: Any,
    auth: AuthConfig,
    host: str = "127.0.0.1",
    *,
    upstream_transport: httpx2.AsyncBaseTransport | None = None,
    metadata_transport: httpx2.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
    clock: Callable[[], float] = time.time,
) -> Starlette:
    from notes_vault_mcp.auth.http import transport_security
    from notes_vault_mcp.dav import WebDav
    from notes_vault_mcp.server import build_server
    from notes_vault_mcp.vault import SubjectVaults

    store = HostedStore(auth.auth_dir or auth_dir())
    provider = HostedProvider(auth, store, ClientMetadataFetcher(metadata_transport, resolver))
    limits = Limits()
    browser = Browser(auth, store, Upstream(auth, upstream_transport), limits)
    credentials = ApiKeyCredentials(store)
    server = build_server(vaults, auth, personal_tokens=credentials, dav_url=auth.dav_url)
    mcp_app = server.streamable_http_app(host=host, transport_security=transport_security(auth))
    prefix = auth.path_prefix
    metadata_document = metadata(auth)

    async def serve_metadata(_: Request) -> Response:
        return JSONResponse(metadata_document, headers={"Cache-Control": "public, max-age=3600"})

    async def health(_: Request) -> Response:
        return PlainTextResponse("ok")

    async def consent_submit(request: Request) -> Response:
        return await browser.consent_submit(request, provider.mint_code)

    registration = RegistrationHandler(
        provider, ClientRegistrationOptions(enabled=True, valid_scopes=list(SCOPES), default_scopes=[READ_SCOPE])
    )
    authenticator = ClientAuthenticator(provider)
    routes: list[BaseRoute] = list(
        create_protected_resource_routes(
            resource_url=URL.validate_python(auth.resource_url),
            authorization_servers=[URL.validate_python(auth.issuer)],
            scopes_supported=list(SCOPES),
        )
    )
    well_known = {AUTHORIZATION_SERVER_PATH + prefix, prefix + AUTHORIZATION_SERVER_PATH}
    routes += [
        Route(path, endpoint=cors(request_response(serve_metadata), ["GET", "OPTIONS"]), methods=["GET", "OPTIONS"])
        for path in sorted(well_known)
    ]
    routes += [
        Route(prefix + "/up", endpoint=health, methods=["GET"]),
        Route(prefix + "/authorize", endpoint=limited(AuthorizationHandler(provider).handle), methods=["GET", "POST"]),
        Route(
            prefix + "/token",
            endpoint=cors(limited(TokenEndpoint(provider).handle), ["POST", "OPTIONS"]),
            methods=["POST", "OPTIONS"],
        ),
        Route(
            prefix + "/register",
            endpoint=cors(limited(registration.handle), ["POST", "OPTIONS"]),
            methods=["POST", "OPTIONS"],
        ),
        Route(
            prefix + "/revoke",
            endpoint=cors(limited(RevocationHandler(provider, authenticator).handle), ["POST", "OPTIONS"]),
            methods=["POST", "OPTIONS"],
        ),
        Route(prefix + "/login", endpoint=browser.consent_page, methods=["GET"]),
        Route(prefix + "/login", endpoint=limited(consent_submit), methods=["POST"]),
        Route(prefix + "/oidc/callback", endpoint=browser.callback, methods=["GET"]),
        Route(prefix + "/account", endpoint=browser.account_page, methods=["GET"]),
        Route(prefix + "/account/keys", endpoint=limited(browser.create_key), methods=["POST"]),
        Route(prefix + "/account/keys/revoke", endpoint=limited(browser.revoke_key), methods=["POST"]),
        Route(prefix + "/logout", endpoint=limited(browser.sign_out), methods=["POST"]),
    ]
    if isinstance(vaults, SubjectVaults):
        routes.append(Mount(prefix + "/dav", app=WebDav(vaults, credentials, mount=prefix + "/dav")))
    routes.append(Mount(prefix or "/", app=mcp_app))
    guard = Middleware(
        HostedGuard,
        provider=provider,
        protected=prefix + "/mcp",
        metadata_url=str(build_resource_metadata_url(URL.validate_python(auth.resource_url))),
    )
    return Starlette(routes=routes, middleware=[guard], lifespan=forwarded_lifespan(mcp_app))
