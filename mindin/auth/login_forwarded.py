from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route
from starlette.types import Scope

from mindin.auth import READ_SCOPE, WRITE_SCOPE, ForwardedConfig
from mindin.auth.hosted_store import Account
from mindin.auth.hosted_web import esc, page
from mindin.auth.identities import Proof
from mindin.auth.login import Login, next_field

ALGORITHMS = ["RS256", "ES256"]


class IdentityError(Exception):
    pass


@dataclass(frozen=True)
class Forwarded:
    proof: Proof
    scopes: list[str]
    expires_at: int


def entitlements(claims: dict[str, Any]) -> set[str]:
    raw = claims.get("entitlements") or []
    named = set(raw.split()) if isinstance(raw, str) else {str(item) for item in raw}
    named.update(str(claims.get("scope") or "").split())
    return named


def header_value(scope: Scope, name: str) -> str:
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == wanted:
            return value.decode("latin-1")
    return ""


class ForwardedSignIn:
    method = "forwarded"

    def __init__(self, login: Login, config: ForwardedConfig) -> None:
        self.login = login
        self.config = config
        login.ambient = self.ambient

    def verify(self, token: str) -> Forwarded:
        try:
            claims = jwt.decode(
                token,
                key=self.config.public_key,
                algorithms=ALGORITHMS,
                issuer=self.config.issuer,
                audience=self.login.config.public_host,
                options={"require": ["exp", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise IdentityError(str(exc)) from exc
        if self.config.typ and claims.get("typ") != self.config.typ:
            raise IdentityError("wrong token type")
        granted = entitlements(claims)
        if granted.intersection(self.config.write_claims):
            scopes = [READ_SCOPE, WRITE_SCOPE]
        elif granted.intersection(self.config.read_claims):
            scopes = [READ_SCOPE]
        else:
            raise IdentityError("no vault entitlement in the forwarded identity")
        email = claims.get("email")
        address = email.strip().lower() if isinstance(email, str) and "@" in email else ""
        proof = Proof(
            method="forwarded",
            subject=str(claims["sub"]),
            emails=(address,) if address else (),
            label=address or str(claims["sub"]),
            name=str(claims.get("name") or ""),
        )
        return Forwarded(proof, scopes, int(claims["exp"]))

    def from_scope(self, scope: Scope) -> Forwarded | None:
        token = header_value(scope, self.config.header)
        if not token:
            return None
        return self.verify(token)

    def account(self, forwarded: Forwarded) -> Account | None:
        known = self.login.identities.account_for("forwarded", forwarded.proof.subject)
        if known is not None:
            return known
        return self.login.identities.sign_in(forwarded.proof, self.login.config.registration).account

    async def ambient(self, request: Request, next_url: str) -> Response | None:
        try:
            forwarded = self.from_scope(request.scope)
        except IdentityError:
            return None
        if forwarded is None:
            return None
        return self.login.complete(request, forwarded.proof, next_url)

    def button(self, next_url: str) -> str:
        return (
            f'<form method="get" action="{esc(self.login.local("/signin/forwarded"))}">{next_field(next_url)}'
            f'<button type="submit">Continue with {esc(self.config.name)}</button></form>'
        )

    def connect_form(self, csrf: str) -> str:
        return ""

    async def start(self, request: Request) -> Response:
        next_url = self.login.next_of(request.query_params.get("next"))
        answered = await self.ambient(request, next_url)
        if answered is None:
            return page("Not signed in", f"This request did not come through {self.config.name}.", status_code=403)
        return answered

    def routes(self, prefix: str) -> list[BaseRoute]:
        return [Route(prefix + "/signin/forwarded", endpoint=self.start, methods=["GET"])]
