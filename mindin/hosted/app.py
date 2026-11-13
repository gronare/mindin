from __future__ import annotations

import copy
import json
import logging
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import anyio
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
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken, ProtectedResourceMetadata
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import BaseRoute, Mount, Route, request_response
from starlette.types import ASGIApp, Receive, Scope, Send

from mindin.changes import NULL_BUS
from mindin.config import ConfigError
from mindin.dav import MAX_BODY_BYTES, WebDav
from mindin.extensions import Extension, HealthCheck, Server, installed, narrowed, sign_in_checks, supplied_bus
from mindin.extensions import access as extended_access
from mindin.hosted.accounts.access import SupportAccess
from mindin.hosted.accounts.index_snapshots import IndexSnapshots
from mindin.hosted.accounts.keys import Keyring, SealedVaults, VaultKeys
from mindin.hosted.accounts.limits import Quotas, limits_document
from mindin.hosted.accounts.reach import ACCOUNT_REACH, AccountReach
from mindin.hosted.accounts.store import Account, ApiKeyCredentials, HostedStore
from mindin.hosted.accounts.vaults import Vault, vault_resource_url
from mindin.hosted.config import READ_SCOPE, SCOPES, WRITE_SCOPE, AuthConfig, auth_dir
from mindin.hosted.feed import ChangeFeed
from mindin.hosted.orgs.pages import OrganisationPages
from mindin.hosted.pages.account import SelfDeletion, SupportDesk
from mindin.hosted.pages.vaults import VaultPages
from mindin.hosted.pages.web import Browser, Limits, limited, oauth_error
from mindin.hosted.periodic import Leases, PeriodicRunner
from mindin.hosted.prefix import URL, inner_lifespan
from mindin.hosted.ready import built_in_checks
from mindin.hosted.signin.cimd import (
    ClientMetadataError,
    ClientMetadataFetcher,
    HostedClient,
    Resolver,
    allowed_redirect,
    is_metadata_url,
)
from mindin.hosted.signin.device import DEVICE_GRANT, DeviceFlow
from mindin.hosted.signin.email import EmailSignIn, Mailer, SmtpFactory
from mindin.hosted.signin.flow import Login
from mindin.hosted.signin.forwarded import ForwardedSignIn, IdentityError
from mindin.hosted.signin.github import GithubSignIn
from mindin.hosted.signin.oidc import OidcSignIn
from mindin.hosted.signin.passkey import PasskeySignIn
from mindin.server import build_server
from mindin.vault import SubjectVaults

logger = logging.getLogger(__name__)

CODE_TTL = 300
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
REQUEST_TTL = 900
AUTHORIZATION_SERVER_PATH = "/.well-known/oauth-authorization-server"


def canonical(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def named_vault(query: str) -> tuple[bool, str | None]:
    pairs = parse_qsl(query, keep_blank_values=True)
    if not pairs:
        return True, None
    if len(pairs) == 1 and pairs[0][0] == "vault":
        return True, pairs[0][1]
    return False, None


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
        extensions: Sequence[Extension] = (),
    ) -> None:
        self.config = config
        self.store = store
        self.fetcher = fetcher
        self.clock = clock
        self.extensions = extensions

    def allowed(self, account: Account, scopes: list[str]) -> list[str]:
        return narrowed(extended_access(self.extensions, account), scopes)

    def resource_target(self, resource: str | None) -> tuple[bool, str | None]:
        if resource is None:
            return True, None
        if canonical(resource) != canonical(self.config.resource_url) or urlsplit(resource.strip()).fragment:
            return False, None
        return named_vault(urlsplit(resource.strip()).query)

    def for_this_resource(self, resource: str | None) -> bool:
        return self.resource_target(resource)[0]

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
        known, slug = self.resource_target(params.resource)
        if not known:
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
                "resource": self.config.resource_url if slug is None else vault_resource_url(self.config, slug),
                "requested_vault": slug,
            },
            REQUEST_TTL,
        )
        return f"{self.config.public_url.rstrip('/')}/login?request={request_id}"

    def mint_code(self, pending: dict[str, Any], scopes: list[str], account: Account, vault: Vault) -> str:
        code = secrets.token_urlsafe(32)
        record = {
            "code": code,
            "client_id": pending["client_id"],
            "scopes": scopes,
            "expires_at": time.time() + CODE_TTL,
            "code_challenge": pending["code_challenge"],
            "redirect_uri": pending["redirect_uri"],
            "redirect_uri_provided_explicitly": pending["redirect_uri_provided_explicitly"],
            "resource": pending.get("resource") or self.config.resource_url,
            "subject": account.sub,
            "account_id": account.id,
            "vault_id": vault.id,
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
        account = self.require_active(record["account_id"])
        vault = self.require_vault(account, record.get("vault_id"))
        return self.issue(client.client_id, account.id, vault.id, record["resource"], authorization_code.scopes, True)

    def require_active(self, account_id: str) -> Account:
        account = self.store.account(account_id)
        if account is None or not account.active:
            raise TokenError("invalid_grant", "the account is not active")
        return account

    def require_vault(self, account: Account, vault_id: str | None) -> Vault:
        vault = self.store.vault(vault_id or "")
        if vault is None or not self.store.reaches(vault, account.id):
            raise TokenError("invalid_grant", "the vault is gone")
        return vault

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
        row = self.store.load_token(refresh_token.token, "refresh")
        if row is None or not self.store.revoke_token(refresh_token.token):
            raise TokenError("invalid_grant", "refresh token does not exist")
        account = self.require_active(refresh_token.subject or "")
        vault = self.require_vault(account, row["vault_id"])
        scopes = scopes or refresh_token.scopes
        return self.issue(client.client_id, account.id, vault.id, row["resource"], scopes, bool(row["spans_account"]))

    def issue(
        self, client_id: str, account_id: str, vault_id: str, resource: str, scopes: list[str], spans_account: bool
    ) -> OAuthToken:
        grant = {
            "grant_id": secrets.token_urlsafe(16),
            "client_id": client_id,
            "account_id": account_id,
            "vault_id": vault_id,
            "scopes": scopes,
            "resource": resource,
            "spans_account": spans_account,
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
        row = self.store.load_token(token, "access")
        return self.authenticate(token, self.resource_target(row["resource"])[1] if row else None).access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revoke_grant(token.token)

    def authenticate(self, token: str, slug: str | None) -> Verdict:
        grant = self.store.key_grant(token)
        if grant is not None:
            if slug is not None and grant.vault.slug != slug:
                return Verdict(None, 403, "wrong_vault", "this key belongs to another vault")
            return self.verdict(
                grant.account,
                grant.vault,
                token,
                f"key:{grant.key_id}",
                grant.scopes,
                None,
                grant.purpose,
                self.config.resource_url if slug is None else vault_resource_url(self.config, slug),
                client=grant.name,
                spans_account=grant.spans_account,
            )
        row = self.store.load_token(token, "access")
        if row is None or row["expires_at"] <= time.time():
            return Verdict(None, 401, "invalid_token", "the token is unknown, revoked or expired")
        known, issued_for = self.resource_target(row["resource"])
        if not known or (issued_for is not None and slug is None):
            return Verdict(None, 401, "invalid_token", "the token was issued for another resource")
        account = self.store.account(row["account_id"])
        if account is None:
            return Verdict(None, 401, "invalid_token", "the token's account is gone")
        bound = self.store.vault(row["vault_id"] or "")
        if bound is None or not self.store.reaches(bound, account.id):
            return Verdict(None, 401, "invalid_token", "the token's vault is gone")
        if slug is not None and bound.slug != slug:
            return Verdict(None, 401, "invalid_token", "the token belongs to another vault")
        return self.verdict(
            account,
            bound,
            token,
            row["client_id"],
            row["scopes"].split(),
            int(row["expires_at"]),
            None,
            row["resource"],
            client=self.store.client_name(row["client_id"]),
            spans_account=bool(row["spans_account"]),
        )

    def verdict(
        self,
        account: Account,
        vault: Vault,
        token: str,
        client_id: str,
        scopes: list[str],
        expires_at: int | None,
        key_purpose: str | None = None,
        resource: str | None = None,
        client: str = "",
        spans_account: bool = False,
    ) -> Verdict:
        if not account.active or not account.sub:
            return Verdict(None, 403, "account_suspended", "the account is not active")
        allowed = self.allowed(account, scopes)
        if not allowed:
            return Verdict(None, 403, "account_restricted", "the account has no access right now")
        access = AccessToken(
            token=token,
            client_id=client_id,
            scopes=allowed,
            expires_at=expires_at,
            resource=resource or self.config.resource_url,
            subject=vault.id,
            claims={
                "email": account.email,
                "account": account.id,
                "vault": vault.slug,
                "vault_name": vault.name,
                "vault_kind": vault.kind,
                "role": "owner" if vault.account_id == account.id else "member",
                "client": client or client_id,
                "key_purpose": key_purpose,
                "reach": ACCOUNT_REACH if spans_account else "",
            },
        )
        return Verdict(access)


@dataclass(frozen=True)
class Target:
    resource: str
    metadata_url: str
    slug: str | None = None


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

    def target(self, path: str, query: str) -> Target | None:
        if path != self.protected and not path.startswith(self.protected + "/"):
            return None
        slug = dict(parse_qsl(query, keep_blank_values=True)).get("vault")
        if slug is None:
            return Target(self.provider.config.resource_url, self.metadata_url)
        named = "?" + vault_resource_url(self.provider.config, slug).partition("?")[2]
        return Target(vault_resource_url(self.provider.config, slug), self.metadata_url + named, slug)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        found = (
            self.target(scope.get("path", ""), scope.get("query_string", b"").decode("latin-1"))
            if scope["type"] == "http"
            else None
        )
        if found is None:
            await self.app(scope, receive, send)
            return
        token = bearer(scope)
        verdict = self.provider.authenticate(token, found.slug) if token else self.through_proxy(scope, found)
        if verdict.access is None:
            await self.refuse(send, verdict, found)
            return
        user = AuthenticatedUser(verdict.access)
        context = auth_context_var.set(user)
        try:
            await self.app({**scope, "user": user, "auth": AuthCredentials(verdict.access.scopes)}, receive, send)
        finally:
            auth_context_var.reset(context)

    def through_proxy(self, scope: Scope, found: Target) -> Verdict:
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
        store = self.provider.store
        vault = store.first_vault(account.id) if found.slug is None else store.vault_at(account.id, found.slug)
        if vault is None:
            return Verdict(None, 403, "wrong_vault", "this vault is not one of the account's")
        return self.provider.verdict(
            account,
            vault,
            "",
            "forwarded",
            forwarded.scopes,
            forwarded.expires_at,
            None,
            found.resource,
            client="company sign-in",
            spans_account=True,
        )

    async def refuse(self, send: Send, verdict: Verdict, found: Target) -> None:
        challenge = f'Bearer resource_metadata="{found.metadata_url}", scope="{" ".join(SCOPES)}"'
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
        login.checks = sign_in_checks(login.store.extensions)
        login.add(EmailSignIn(login, Mailer(auth.smtp, smtp_factory)))
    if auth.github:
        login.add(GithubSignIn(login, auth.github, github_transport))
    if auth.oidc:
        login.add(OidcSignIn(login, auth.oidc, oidc_transport))
    return forwarded


def passes(check: HealthCheck) -> bool:
    try:
        return bool(check())
    except Exception:
        logger.exception("mindin: a readiness check failed")
        return False


def failing_checks(checks: Sequence[HealthCheck]) -> list[str]:
    return [getattr(check, "__name__", repr(check)) for check in checks if not passes(check)]


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
    extensions: Sequence[Extension] | None = None,
) -> Starlette:

    extensions = installed() if extensions is None else tuple(extensions)
    store = HostedStore(auth.auth_dir or auth_dir(), extensions)
    if not isinstance(vaults, SubjectVaults):
        raise ConfigError("mindin: the HTTP server serves one sealed vault per account")
    keys = VaultKeys(store, keyring or Keyring.from_env())
    keys.verify()
    sealed = SealedVaults(store, keys, vaults.prefix)
    sealed.quotas = Quotas(store, extensions, lambda vault_id: sealed.facts(vault_id).size)
    vaults.use_backends(sealed, identify=sealed.identifier)
    vaults.use_feed(ChangeFeed(store, lambda vault_id: sealed.for_vault(vault_id)[0]))
    if auth.index_snapshots:
        vaults.use_snapshots(IndexSnapshots(sealed), auth.snapshot_minutes * 60.0)
    vaults.use_sweep(sealed.live_identifiers)
    vaults.use_reach(AccountReach(store))
    provider = HostedProvider(auth, store, ClientMetadataFetcher(metadata_transport, resolver), clock, extensions)
    limits = Limits(store)
    browser = Browser(auth, store, limits, clock)
    login = Login(auth, store, browser, limits)
    forwarded = sign_in_methods(auth, login, oidc_transport, github_transport, smtp_factory)
    device = DeviceFlow(auth, store, provider.get_client, browser, limits, clock)
    credentials = ApiKeyCredentials(store, provider.allowed)
    support = SupportDesk(browser, SupportAccess(store, clock))
    deletion = SelfDeletion(browser, vaults, sealed)
    pages = VaultPages(browser, vaults, sealed, deletion, smtp_factory)
    organisation_pages = OrganisationPages(pages)
    extended = Server(auth, store, clock, browser, vaults, sealed, deletion, smtp_factory)
    for extension in extensions:
        extension.attach(extended)
    vaults.use_bus(supplied_bus(extensions, extended) or NULL_BUS)
    pages.sections += extended.vault_sections
    organisation_pages.sections += extended.organisation_sections
    built_in = built_in_checks(store)

    def extended_and_support(account: Account, csrf: str) -> str:
        added = "".join(section(account, csrf) for section in extended.account_sections)
        return f'<div class="cols">{added}{support.section(account, csrf)}</div>'

    browser.sections += [organisation_pages.section, extended_and_support, deletion.section]
    server = build_server(vaults, personal_tokens=credentials, dav_url=auth.dav_url, machine_tools=True)
    mcp_app = server.streamable_http_app(
        host=host, transport_security=transport_security(auth), stateless_http=auth.stateless_http
    )
    prefix = auth.path_prefix
    metadata_document = metadata(auth)

    async def serve_metadata(_: Request) -> Response:
        return JSONResponse(metadata_document, headers={"Cache-Control": "public, max-age=3600"})

    async def health(_: Request) -> Response:
        return PlainTextResponse("ok")

    async def ready(_: Request) -> Response:
        failing = await anyio.to_thread.run_sync(failing_checks, [*built_in, *extended.health_checks])
        status = 503 if failing else 200
        return JSONResponse({"ready": not failing, "failing": failing}, status, {"Cache-Control": "no-store"})

    def answer_limits(secret: str) -> Response:
        grant = store.key_grant(secret) if secret else None
        owner = store.account_by_id(grant.vault.account_id) if grant else None
        if grant is None or owner is None or not grant.account.active or not grant.account.sub:
            return JSONResponse({"error": "invalid_token"}, 401, {"WWW-Authenticate": "Bearer"})
        scopes = provider.allowed(grant.account, grant.scopes)
        if not scopes:
            return JSONResponse({"error": "insufficient_scope"}, 403)
        document = limits_document(sealed.quotas, grant.vault, owner, MAX_BODY_BYTES) | {"write": WRITE_SCOPE in scopes}
        return JSONResponse(document, headers={"Cache-Control": "no-store"})

    async def vault_limits(request: Request) -> Response:
        return await anyio.to_thread.run_sync(answer_limits, bearer(request.scope))

    async def consent_submit(request: Request) -> Response:
        return await browser.consent_submit(request, provider.mint_code)

    async def resource_metadata(request: Request) -> Response:
        slug = request.query_params.get("vault")
        document = ProtectedResourceMetadata(
            resource=URL.validate_python(auth.resource_url if slug is None else vault_resource_url(auth, slug)),
            authorization_servers=[URL.validate_python(auth.issuer)],
            scopes_supported=list(SCOPES),
        )
        return JSONResponse(
            document.model_dump(mode="json", exclude_none=True), headers={"Cache-Control": "public, max-age=3600"}
        )

    registration = RegistrationHandler(
        provider, ClientRegistrationOptions(enabled=True, valid_scopes=list(SCOPES), default_scopes=[READ_SCOPE])
    )
    authenticator = ClientAuthenticator(provider)
    resource_metadata_url = str(build_resource_metadata_url(URL.validate_python(auth.resource_url)))
    routes: list[BaseRoute] = [
        Route(
            urlsplit(resource_metadata_url).path,
            endpoint=cors(request_response(resource_metadata), ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        )
    ]
    well_known = {AUTHORIZATION_SERVER_PATH + prefix, prefix + AUTHORIZATION_SERVER_PATH}
    routes += [
        Route(path, endpoint=cors(request_response(serve_metadata), ["GET", "OPTIONS"]), methods=["GET", "OPTIONS"])
        for path in sorted(well_known)
    ]
    routes += [
        Route(prefix + "/up", endpoint=health, methods=["GET"]),
        Route(prefix + "/ready", endpoint=ready, methods=["GET"]),
        Route(prefix + "/api/vault/limits", endpoint=vault_limits, methods=["GET"]),
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
        Route(prefix + "/account/settings", endpoint=browser.settings_page, methods=["GET"]),
        Route(prefix + "/logout", endpoint=limited(browser.sign_out), methods=["POST"]),
    ]
    routes += login.routes(prefix) + pages.routes(prefix, limited) + support.routes(prefix, limited)
    routes += deletion.routes(prefix, limited) + organisation_pages.routes(prefix, limited) + extended.routes
    routes.append(Mount(prefix + "/dav", app=WebDav(vaults, credentials, mount=prefix + "/dav")))
    routes.append(Mount(prefix or "/", app=mcp_app))
    guard = Middleware(
        HostedGuard,
        provider=provider,
        protected=prefix + "/mcp",
        metadata_url=resource_metadata_url,
        forwarded=forwarded,
    )
    periodic = PeriodicRunner([organisation_pages.periodic(), *extended.periodic], Leases(store, clock).claim)

    def close() -> None:
        periodic.stop()
        vaults.close()

    lifespan = inner_lifespan(mcp_app, close, on_start=periodic.start)
    return Starlette(routes=routes, middleware=[guard], lifespan=lifespan)


class WithoutQuery(logging.Filter):
    """Drops the query string from uvicorn's access lines, where sign-in links carry their tokens."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].partition("?")[0], *args[3:])
        return True


def uvicorn_options(auth: AuthConfig) -> dict[str, Any]:
    from uvicorn.config import LOGGING_CONFIG

    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config["filters"] = {"without_query": {"()": WithoutQuery}}
    log_config["handlers"]["access"]["filters"] = ["without_query"]
    options: dict[str, Any] = {"log_level": "info", "log_config": log_config, "proxy_headers": True}
    if auth.forwarded_allow_ips:
        options["forwarded_allow_ips"] = list(auth.forwarded_allow_ips)
    return options


def run_http(vaults: SubjectVaults, host: str, port: int, auth: AuthConfig) -> None:
    import uvicorn

    uvicorn.run(build_hosted_app(vaults, auth, host), host=host, port=port, **uvicorn_options(auth))
