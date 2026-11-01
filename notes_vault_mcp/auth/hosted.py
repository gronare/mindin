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
from mcp.server.transport_security import RequestBodyLimitMiddleware, TransportSecuritySettings
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
from notes_vault_mcp.auth.billing import BillingApi, Handoff, effective_scopes
from notes_vault_mcp.auth.cimd import (
    ClientMetadataError,
    ClientMetadataFetcher,
    HostedClient,
    Resolver,
    allowed_redirect,
    is_metadata_url,
)
from notes_vault_mcp.auth.device import DEVICE_GRANT, DeviceFlow
from notes_vault_mcp.auth.hosted_access import SupportAccess, SupportDesk
from notes_vault_mcp.auth.hosted_deletion import SelfDeletion
from notes_vault_mcp.auth.hosted_keys import AccountKeys, Keyring, SealedVaults
from notes_vault_mcp.auth.hosted_layout import Layouts
from notes_vault_mcp.auth.hosted_store import Account, ApiKeyCredentials, HostedStore
from notes_vault_mcp.auth.hosted_transfer import VaultTransfer
from notes_vault_mcp.auth.hosted_web import Browser, Limits, oauth_error
from notes_vault_mcp.auth.login import Login
from notes_vault_mcp.auth.login_email import EmailSignIn, Mailer, SmtpFactory
from notes_vault_mcp.auth.login_forwarded import ForwardedSignIn, IdentityError
from notes_vault_mcp.auth.login_github import GithubSignIn
from notes_vault_mcp.auth.login_oidc import OidcSignIn
from notes_vault_mcp.auth.login_passkey import PasskeySignIn
from notes_vault_mcp.auth.prefix import URL, inner_lifespan
from notes_vault_mcp.config import ConfigError

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
    def __init__(
        self,
        config: AuthConfig,
        store: HostedStore,
        fetcher: ClientMetadataFetcher,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.store = store
        self.fetcher = fetcher
        self.clock = clock

    def allowed(self, account: Account, scopes: list[str]) -> list[str]:
        return effective_scopes(self.config, account, scopes, self.clock())

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
        allowed = self.allowed(account, scopes)
        if not allowed:
            return Verdict(None, 403, "plan-inactive", "the account's plan has ended")
        access = AccessToken(
            token=token,
            client_id=client_id,
            scopes=allowed,
            expires_at=expires_at,
            resource=self.config.resource_url,
            subject=account.sub,
            claims={"email": account.email, "account": account.id},
        )
        return Verdict(access)


class HostedGuard:
    def __init__(
        self,
        app: ASGIApp,
        provider: HostedProvider,
        protected: str,
        metadata_url: str,
        forwarded: ForwardedSignIn | None = None,
    ) -> None:
        self.app = app
        self.provider = provider
        self.protected = protected
        self.metadata_url = metadata_url
        self.forwarded = forwarded

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == self.protected or path.startswith(self.protected + "/")):
            await self.app(scope, receive, send)
            return
        token = bearer(scope)
        verdict = self.provider.authenticate(token) if token else self.through_proxy(scope)
        if verdict.access is None:
            await self.refuse(send, verdict)
            return
        user = AuthenticatedUser(verdict.access)
        context = auth_context_var.set(user)
        try:
            await self.app({**scope, "user": user, "auth": AuthCredentials(verdict.access.scopes)}, receive, send)
        finally:
            auth_context_var.reset(context)

    def through_proxy(self, scope: Scope) -> Verdict:
        if self.forwarded is None:
            return Verdict(None, 401)
        try:
            forwarded = self.forwarded.from_scope(scope)
        except IdentityError as exc:
            return Verdict(None, 403, "invalid_token", str(exc))
        if forwarded is None:
            return Verdict(None, 401)
        account = self.forwarded.account(forwarded)
        if account is None:
            return Verdict(None, 403, "no_account", "the forwarded identity has no account on this vault")
        return self.provider.verdict(account, "", "forwarded", forwarded.scopes, forwarded.expires_at)

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
        "device_authorization_endpoint": f"{base}/device_authorization",
        "scopes_supported": list(SCOPES),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token", DEVICE_GRANT],
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
    def __init__(self, provider: HostedProvider, device: DeviceFlow) -> None:
        self.provider = provider
        self.device = device
        self.handler = TokenHandler(provider, ClientAuthenticator(provider))

    async def handle(self, request: Request) -> Response:
        form = await request.form()
        if form.get("grant_type") == DEVICE_GRANT:
            return await self.device.token(request, form)
        resource = form.get("resource")
        if isinstance(resource, str) and not self.provider.for_this_resource(resource):
            return oauth_error(
                "invalid_target", f"this server only issues tokens for {self.provider.config.resource_url}"
            )
        return await self.handler.handle(request)


LOCAL_HOSTS = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"]
LOCAL_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def transport_security(auth: AuthConfig) -> TransportSecuritySettings | None:
    hosts: list[str] = []
    origins: list[str] = []
    if auth.public_host:
        hosts += [auth.public_host, f"{auth.public_host}:*"]
    if auth.public_url and "://" in auth.public_url:
        origins.append(auth.public_origin)
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


def sign_in_methods(
    auth: AuthConfig,
    login: Login,
    oidc_transport: httpx2.AsyncBaseTransport | None,
    github_transport: httpx2.AsyncBaseTransport | None,
    smtp_factory: SmtpFactory | None,
) -> ForwardedSignIn | None:
    forwarded = ForwardedSignIn(login, auth.forwarded) if auth.forwarded else None
    if forwarded:
        login.add(forwarded)
    if auth.passkeys:
        login.add(PasskeySignIn(login))
    if auth.smtp:
        login.add(EmailSignIn(login, Mailer(auth.smtp, smtp_factory)))
    if auth.github:
        login.add(GithubSignIn(login, auth.github, github_transport))
    if auth.oidc:
        login.add(OidcSignIn(login, auth.oidc, oidc_transport))
    return forwarded


def build_hosted_app(
    vaults: Any,
    auth: AuthConfig,
    host: str = "127.0.0.1",
    *,
    oidc_transport: httpx2.AsyncBaseTransport | None = None,
    github_transport: httpx2.AsyncBaseTransport | None = None,
    smtp_factory: SmtpFactory | None = None,
    metadata_transport: httpx2.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
    clock: Callable[[], float] = time.time,
    keyring: Keyring | None = None,
) -> Starlette:
    from notes_vault_mcp.dav import WebDav
    from notes_vault_mcp.server import build_server
    from notes_vault_mcp.vault import SubjectVaults

    store = HostedStore(auth.auth_dir or auth_dir())
    if not isinstance(vaults, SubjectVaults):
        raise ConfigError("notes-vault-mcp: the HTTP server serves one sealed vault per account")
    keys = AccountKeys(store, keyring or Keyring.from_env())
    keys.verify()
    sealed = SealedVaults(store, keys, vaults.prefix)
    vaults.use_backends(sealed)
    provider = HostedProvider(auth, store, ClientMetadataFetcher(metadata_transport, resolver), clock)
    limits = Limits()
    browser = Browser(auth, store, limits, Handoff(auth, clock), clock)
    login = Login(auth, store, browser, limits)
    forwarded = sign_in_methods(auth, login, oidc_transport, github_transport, smtp_factory)
    device = DeviceFlow(auth, store, provider.get_client, browser, limits, clock)
    credentials = ApiKeyCredentials(store, provider.allowed)
    billing = BillingApi(auth, store, clock)
    transfer = VaultTransfer(browser, vaults)
    support = SupportDesk(browser, SupportAccess(store, clock))
    deletion = SelfDeletion(browser, vaults, sealed)
    layouts = Layouts(browser, vaults)
    browser.sections += [layouts.section, transfer.section, support.section, deletion.section]
    server = build_server(vaults, personal_tokens=credentials, dav_url=auth.dav_url)
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
            endpoint=cors(limited(TokenEndpoint(provider, device).handle), ["POST", "OPTIONS"]),
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
        Route(
            prefix + "/device_authorization",
            endpoint=cors(limited(device.start), ["POST", "OPTIONS"]),
            methods=["POST", "OPTIONS"],
        ),
        Route(prefix + "/device", endpoint=device.page, methods=["GET"]),
        Route(prefix + "/device", endpoint=limited(device.submit), methods=["POST"]),
        Route(prefix + "/login", endpoint=browser.consent_page, methods=["GET"]),
        Route(prefix + "/login", endpoint=limited(consent_submit), methods=["POST"]),
        Route(prefix + "/account", endpoint=browser.account_page, methods=["GET"]),
        Route(prefix + "/account/keys", endpoint=limited(browser.create_key), methods=["POST"]),
        Route(prefix + "/account/keys/revoke", endpoint=limited(browser.revoke_key), methods=["POST"]),
        Route(prefix + "/logout", endpoint=limited(browser.sign_out), methods=["POST"]),
        Route(prefix + "/account/billing/{purpose}", endpoint=browser.billing, methods=["GET"]),
        Route(prefix + "/v1/admin/accounts", endpoint=billing.list_accounts, methods=["GET"]),
        Route(prefix + "/v1/admin/accounts/{account}", endpoint=billing.show, methods=["GET"]),
        Route(prefix + "/v1/admin/accounts/{account}/plan", endpoint=limited(billing.set_plan), methods=["PUT"]),
        Route(prefix + "/v1/admin/accounts/{account}/deactivate", endpoint=billing.deactivate, methods=["POST"]),
        Route(prefix + "/v1/admin/accounts/{account}/activate", endpoint=billing.activate, methods=["POST"]),
    ]
    routes += login.routes(prefix) + transfer.routes(prefix) + support.routes(prefix, limited)
    routes += deletion.routes(prefix, limited) + layouts.routes(prefix, limited)
    routes.append(Mount(prefix + "/dav", app=WebDav(vaults, credentials, mount=prefix + "/dav")))
    routes.append(Mount(prefix or "/", app=mcp_app))
    guard = Middleware(
        HostedGuard,
        provider=provider,
        protected=prefix + "/mcp",
        metadata_url=str(build_resource_metadata_url(URL.validate_python(auth.resource_url))),
        forwarded=forwarded,
    )
    return Starlette(routes=routes, middleware=[guard], lifespan=inner_lifespan(mcp_app))
