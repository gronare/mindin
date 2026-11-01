from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Literal
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from notes_vault_mcp.auth import READ_SCOPE, AuthConfig
from notes_vault_mcp.auth.hosted_store import Account, HostedStore
from notes_vault_mcp.config import ConfigError

HANDOFF_DOMAIN = "vault-billing-handoff-v1"
HANDOFF_SECONDS = 540
HANDOFF_PATHS = {"checkout": "checkout", "portal": "billing_portal"}
BILLING_SCOPE = "billing"
MAX_UNTIL = 2**53 - 1
LIST_LIMIT = 500
Access = Literal["full", "read", "none"]


def handoff_secret(text: str) -> bytes:
    raw = text.strip().rstrip("=")
    try:
        secret = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ConfigError("notes-vault-mcp: VAULT_BILLING_HANDOFF_SECRET is not base64url") from exc
    if len(secret) != 32:
        raise ConfigError("notes-vault-mcp: VAULT_BILLING_HANDOFF_SECRET must be 32 bytes, base64url")
    return secret


def signature(secret: bytes, purpose: str, account: str, expires: int, domain: str = HANDOFF_DOMAIN) -> str:
    message = domain.encode() + purpose.encode() + b"\0" + account.encode() + b"\0" + str(expires).encode()
    return base64.urlsafe_b64encode(hmac.new(secret, message, hashlib.sha256).digest()).decode().rstrip("=")


def handoff_link(base: str, secret: bytes, purpose: str, account: str, expires: int) -> str:
    sign = signature(secret, purpose, account, expires)
    return f"{base.rstrip('/')}/{HANDOFF_PATHS[purpose]}?account={quote(account)}&expires={expires}&signature={sign}"


def lapse_moment(account: Account) -> int | None:
    if account.plan_status == "active":
        return account.plan_until
    if account.plan_until is not None and account.plan_until < account.plan_set_at:
        return account.plan_until
    return account.plan_set_at


def entitled(account: Account, at: float) -> bool:
    return account.plan_status == "active" and (account.plan_until is None or account.plan_until > at)


def plan_access(config: AuthConfig, account: Account, at: float) -> Access:
    if not config.plans or entitled(account, at):
        return "full"
    lapsed = lapse_moment(account)
    if lapsed is not None and at < lapsed + config.plan_read_only_days * 86400:
        return "read"
    return "none"


def effective_scopes(config: AuthConfig, account: Account, scopes: list[str], at: float) -> list[str]:
    access = plan_access(config, account, at)
    if access == "full":
        return scopes
    return [scope for scope in scopes if scope == READ_SCOPE] if access == "read" else []


def plan_view(config: AuthConfig, account: Account, at: float) -> dict:
    return {
        "status": account.plan_status,
        "until": account.plan_until,
        "entitled": not config.plans or entitled(account, at),
    }


def refusal(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers={"Cache-Control": "no-store"})


class BillingApi:
    def __init__(self, config: AuthConfig, store: HostedStore, clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.store = store
        self.clock = clock

    def authorize(self, request: Request) -> JSONResponse | None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        scope = self.store.admin_token_scope(token.strip()) if scheme.lower() == "bearer" else None
        if scope is None:
            return refusal(401, "unauthorized")
        if scope != BILLING_SCOPE:
            return refusal(403, "wrong-scope")
        return None

    def summary(self, account: Account) -> dict:
        return {
            "account": account.id,
            "email": account.email,
            "status": account.status,
            "created_at": account.created_at,
            "deactivated_at": account.status_changed_at if account.status == "suspended" else None,
            "plan": plan_view(self.config, account, self.clock()),
            "keys": len(self.store.keys(account.id)),
            "last_seen": self.store.last_seen(account.id),
        }

    async def list_accounts(self, request: Request) -> Response:
        if (refused := self.authorize(request)) is not None:
            return refused
        raw_limit = request.query_params.get("limit", str(LIST_LIMIT))
        if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= LIST_LIMIT:
            return refusal(400, "limit-out-of-range")
        accounts = self.store.accounts_page(request.query_params.get("after"), int(raw_limit))
        more = len(accounts) > int(raw_limit)
        page = accounts[: int(raw_limit)]
        return JSONResponse(
            {"accounts": [self.summary(account) for account in page], "next": page[-1].id if more and page else None}
        )

    async def show(self, request: Request) -> Response:
        if (refused := self.authorize(request)) is not None:
            return refused
        account = self.store.account_by_id(request.path_params["account"])
        return JSONResponse(self.summary(account)) if account else refusal(404, "not-found")

    async def set_plan(self, request: Request) -> Response:
        if (refused := self.authorize(request)) is not None:
            return refused
        try:
            update = json.loads(await request.body())
        except ValueError:
            return refusal(400, "bad-request")
        status, until = (update.get("status"), update.get("until")) if isinstance(update, dict) else (None, None)
        if status not in ("active", "inactive") or not (until is None or type(until) is int):
            return refusal(400, "bad-request")
        if until is not None and not 0 <= until <= MAX_UNTIL:
            return refusal(400, "until-out-of-range")
        account = self.store.set_plan(request.path_params["account"], status, until, int(self.clock()))
        if account is None:
            return refusal(404, "not-found")
        return JSONResponse(
            {"account": account.id, "email": account.email, "plan": plan_view(self.config, account, self.clock())}
        )

    async def deactivate(self, request: Request) -> Response:
        return self.change(request, "suspended")

    async def activate(self, request: Request) -> Response:
        return self.change(request, "active")

    def change(self, request: Request, status: Literal["active", "suspended"]) -> Response:
        if (refused := self.authorize(request)) is not None:
            return refused
        account = self.store.account_by_id(request.path_params["account"])
        if account is None:
            return refusal(404, "not-found")
        self.store.set_status(account.id, status, source=BILLING_SCOPE)
        return Response(status_code=204)


class Handoff:
    def __init__(self, config: AuthConfig, clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.clock = clock

    @property
    def configured(self) -> bool:
        return bool(self.config.billing_url and self.config.billing_handoff_secret)

    def link(self, purpose: str, account: Account) -> str:
        secret = handoff_secret(self.config.billing_handoff_secret)
        return handoff_link(self.config.billing_url, secret, purpose, account.id, int(self.clock()) + HANDOFF_SECONDS)

    def redirect(self, purpose: str, account: Account) -> Response:
        if purpose not in HANDOFF_PATHS or not self.configured:
            return Response("not found", status_code=404)
        return RedirectResponse(self.link(purpose, account), status_code=303, headers={"Cache-Control": "no-store"})
