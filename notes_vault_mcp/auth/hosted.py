from __future__ import annotations

import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Sequence
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

from notes_vault_mcp.auth import READ_SCOPE, SCOPES, AuthConfig, auth_dir
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
from notes_vault_mcp.auth.hosted_keys import Keyring, SealedVaults, VaultKeys
from notes_vault_mcp.auth.hosted_store import Account, ApiKeyCredentials, HostedStore
from notes_vault_mcp.auth.hosted_vault_pages import VaultPages
from notes_vault_mcp.auth.hosted_vaults import Vault
from notes_vault_mcp.auth.hosted_web import Browser, Limits, oauth_error
from notes_vault_mcp.auth.login import Login
from notes_vault_mcp.auth.login_email import EmailSignIn, Mailer, SmtpFactory
from notes_vault_mcp.auth.login_forwarded import ForwardedSignIn, IdentityError
from notes_vault_mcp.auth.login_github import GithubSignIn
from notes_vault_mcp.auth.login_oidc import OidcSignIn
from notes_vault_mcp.auth.login_passkey import PasskeySignIn
from notes_vault_mcp.auth.prefix import URL, inner_lifespan
from notes_vault_mcp.config import ConfigError
from notes_vault_mcp.extensions import Extension, Server, installed, narrowed
from notes_vault_mcp.extensions import access as extended_access

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
        extensions: Sequence[Extension] = (),
    ) -> None:
        self.config = config
        self.store = store
        self.fetcher = fetcher
        self.clock = clock
        self.extensions = extensions

    def allowed(self, account: Account, scopes: list[str]) -> list[str]:
        return narrowed(extended_access(self.extensions, account), scopes)

    def vault_resource(self, vault: Vault) -> str:
        return vault_resource_url(self.config, vault.slug)

    def resource_target(self, resource: str | None) -> tuple[bool, Vault | None]:
        if resource is None or canonical(resource) == canonical(self.config.resource_url):
            return True, None
        wanted = canonical(resource)
        base = canonical(self.config.public_url) + "/v/"
        slug = wanted[len(base) :].removesuffix("/mcp") if wanted.startswith(base) and wanted.endswith("/mcp") else ""
        vault = self.store.vault_by_slug(slug) if slug and "/" not in slug else None
        return vault is not None, vault

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
        known, vault = self.resource_target(params.resource)
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
                "resource": self.vault_resource(vault) if vault else self.config.resource_url,
                "vault_id": vault.id if vault else None,
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
        return self.issue(client.client_id, account.id, vault.id, record["resource"], authorization_code.scopes)

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
        return self.issue(client.client_id, account.id, vault.id, row["resource"], scopes or refresh_token.scopes)

    def issue(self, client_id: str, account_id: str, vault_id: str, resource: str, scopes: list[str]) -> OAuthToken:
        grant = {
            "grant_id": secrets.token_urlsafe(16),
            "client_id": client_id,
            "account_id": account_id,
            "vault_id": vault_id,
            "scopes": scopes,
            "resource": resource,
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
        return self.authenticate(token, None).access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revoke_grant(token.token)

    def authenticate(self, token: str, resource: str | None, vault: Vault | None = None) -> Verdict:
        grant = self.store.key_grant(token)
        if grant is not None:
            if vault is not None and grant.vault.id != vault.id:
                return Verdict(None, 403, "wrong_vault", "this key belongs to another vault")
            return self.verdict(
                grant.account,
                grant.vault,
                token,
                f"key:{grant.key_id}",
                grant.scopes,
                None,
                grant.purpose,
                resource,
                client=grant.name,
            )
        row = self.store.load_token(token, "access")
        if row is None or row["expires_at"] <= time.time():
            return Verdict(None, 401, "invalid_token", "the token is unknown, revoked or expired")
        if resource is not None and canonical(row["resource"]) != canonical(resource):
            return Verdict(None, 401, "invalid_token", "the token was issued for another resource")
        account = self.store.account(row["account_id"])
        if account is None:
            return Verdict(None, 401, "invalid_token", "the token's account is gone")
        bound = self.store.vault(row["vault_id"] or "")
        if bound is None or (vault is not None and bound.id != vault.id) or not self.store.reaches(bound, account.id):
            return Verdict(None, 401, "invalid_token", "the token's vault is gone")
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
            },
        )
        return Verdict(access)


@dataclass(frozen=True)
class Target:
    resource: str
    metadata_url: str
    path: str
    vault: Vault | None = None


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
        self.vault_paths = re.compile(re.escape(protected.removesuffix("/mcp")) + r"/v/([^/]+)/mcp(/.*)?")
        self.forwarded = forwarded

    def target(self, path: str) -> Target | None | Response:
        if path == self.protected or path.startswith(self.protected + "/"):
            return Target(self.provider.config.resource_url, self.metadata_url, path)
        matched = self.vault_paths.fullmatch(path)
        if matched is None:
            return None
        vault = self.provider.store.vault_by_slug(matched.group(1))
        if vault is None:
            return JSONResponse({"error": "not_found", "error_description": "there is no such vault"}, 404)
        resource = self.provider.vault_resource(vault)
        metadata_url = str(build_resource_metadata_url(URL.validate_python(resource)))
        return Target(resource, metadata_url, self.protected + (matched.group(2) or ""), vault)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        found = self.target(scope.get("path", "")) if scope["type"] == "http" else None
        if found is None:
            await self.app(scope, receive, send)
            return
        if isinstance(found, Response):
            await found(scope, receive, send)
            return
        token = bearer(scope)
        verdict = (
            self.provider.authenticate(token, found.resource, found.vault)
            if token
            else self.through_proxy(scope, found)
        )
        if verdict.access is None:
            await self.refuse(send, verdict, found)
            return
        user = AuthenticatedUser(verdict.access)
        context = auth_context_var.set(user)
        inner = {**scope, "user": user, "auth": AuthCredentials(verdict.access.scopes)}
        if found.vault is not None:
            inner |= {"path": found.path, "raw_path": found.path.encode("utf-8")}
        try:
            await self.app(inner, receive, send)
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
        vault = found.vault or self.provider.store.first_vault(account.id)
        if vault is None or not self.provider.store.reaches(vault, account.id):
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


def vault_resource_url(config: AuthConfig, slug: str) -> str:
    return f"{config.public_url.rstrip('/')}/v/{slug}/mcp"


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
    extensions: Sequence[Extension] | None = None,
) -> Starlette:
    from notes_vault_mcp.dav import WebDav
    from notes_vault_mcp.server import build_server
    from notes_vault_mcp.vault import SubjectVaults

    extensions = installed() if extensions is None else tuple(extensions)
    store = HostedStore(auth.auth_dir or auth_dir(), extensions)
    if not isinstance(vaults, SubjectVaults):
        raise ConfigError("notes-vault-mcp: the HTTP server serves one sealed vault per account")
    keys = VaultKeys(store, keyring or Keyring.from_env())
    keys.verify()
    sealed = SealedVaults(store, keys, vaults.prefix)
    vaults.use_backends(sealed)
    provider = HostedProvider(auth, store, ClientMetadataFetcher(metadata_transport, resolver), clock, extensions)
    limits = Limits()
    browser = Browser(auth, store, limits, clock)
    login = Login(auth, store, browser, limits)
    forwarded = sign_in_methods(auth, login, oidc_transport, github_transport, smtp_factory)
    device = DeviceFlow(auth, store, provider.get_client, browser, limits, clock)
    credentials = ApiKeyCredentials(store, provider.allowed)
    support = SupportDesk(browser, SupportAccess(store, clock))
    deletion = SelfDeletion(browser, vaults, sealed)
    pages = VaultPages(browser, vaults, sealed, deletion, smtp_factory)
    extended = Server(auth, store, clock, browser, vaults, sealed, deletion, smtp_factory)
    for extension in extensions:
        extension.attach(extended)
    pages.sections += extended.vault_sections

    def extended_and_support(account: Account, csrf: str) -> str:
        added = "".join(section(account, csrf) for section in extended.account_sections)
        return f'<div class="cols">{added}{support.section(account, csrf)}</div>'

    browser.sections += [extended_and_support, deletion.section]
    server = build_server(vaults, personal_tokens=credentials, dav_url=auth.dav_url, machine_tools=True)
    mcp_app = server.streamable_http_app(host=host, transport_security=transport_security(auth))
    prefix = auth.path_prefix
    metadata_document = metadata(auth)

    async def serve_metadata(_: Request) -> Response:
        return JSONResponse(metadata_document, headers={"Cache-Control": "public, max-age=3600"})

    async def health(_: Request) -> Response:
        return PlainTextResponse("ok")

    async def consent_submit(request: Request) -> Response:
        return await browser.consent_submit(request, provider.mint_code)

    async def vault_metadata(request: Request) -> Response:
        vault = store.vault_by_slug(request.path_params["slug"])
        if vault is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        document = ProtectedResourceMetadata(
            resource=URL.validate_python(provider.vault_resource(vault)),
            authorization_servers=[URL.validate_python(auth.issuer)],
            scopes_supported=list(SCOPES),
        )
        return JSONResponse(
            document.model_dump(mode="json", exclude_none=True), headers={"Cache-Control": "public, max-age=300"}
        )

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
    routes.append(
        Route(
            f"/.well-known/oauth-protected-resource{prefix}/v/{{slug}}/mcp",
            endpoint=cors(request_response(vault_metadata), ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        )
    )
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
        Route(prefix + "/account/settings", endpoint=browser.settings_page, methods=["GET"]),
        Route(prefix + "/logout", endpoint=limited(browser.sign_out), methods=["POST"]),
    ]
    routes += login.routes(prefix) + pages.routes(prefix, limited) + support.routes(prefix, limited)
    routes += deletion.routes(prefix, limited) + extended.routes
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
