from __future__ import annotations

import logging
import secrets

import httpx2
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from mindin.hosted.config import OidcConfig
from mindin.hosted.pages.html import esc, hidden_field
from mindin.hosted.pages.web import client_address, page
from mindin.hosted.signin.flow import Login, next_field
from mindin.hosted.signin.identities import Proof
from mindin.hosted.signin.upstream import Upstream, UpstreamError

logger = logging.getLogger(__name__)


class OidcSignIn:
    method = "oidc"

    def __init__(self, login: Login, config: OidcConfig, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self.login = login
        self.config = config
        self.upstream = Upstream(config, login.config.oidc_redirect_uri, transport)

    def button(self, next_url: str) -> str:
        return (
            f'<form method="get" action="{esc(self.login.local("/signin/oidc"))}">{next_field(next_url)}'
            f'<button type="submit">Continue with {esc(self.config.name)}</button></form>'
        )

    def connect_form(self, csrf: str) -> str:
        return (
            f'<form method="post" action="{esc(self.login.local("/signin/oidc"))}">{hidden_field("csrf", csrf)}'
            f'<button type="submit" class="secondary">Add {esc(self.config.name)}</button></form>'
        )

    async def start(self, request: Request) -> Response:
        link_account = ""
        next_url = self.login.next_of(request.query_params.get("next"))
        if request.method == "POST":
            form = {key: str(value) for key, value in (await request.form()).items()}
            linked = self.login.linking(request, form)
            if isinstance(linked, Response):
                return linked
            link_account, next_url = linked, self.login.local("/account/settings")
        if not self.login.limits.sign_in.allow(client_address(request)):
            return page("Slow down", "Too many sign-ins from here. Try again in a minute.", status_code=429)
        nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(32)
        try:
            target = await self.upstream.authorization_url(state, nonce, verifier)
        except UpstreamError as exc:
            logger.warning("oidc sign-in: %s", exc)
            return page("Sign-in is unavailable", "The identity provider cannot be reached.", status_code=502)
        document = {"nonce": nonce, "verifier": verifier, "next": next_url, "link": link_account}
        return self.login.begin("oidc", document, target, state=state)

    async def callback(self, request: Request) -> Response:
        resumed = self.login.resume(request, "oidc")
        if isinstance(resumed, Response):
            return resumed
        if request.query_params.get("error"):
            return page("Sign-in cancelled", "The identity provider did not sign you in.", status_code=400)
        try:
            identity = await self.upstream.identity(
                request.query_params.get("code", ""), resumed["verifier"], resumed["nonce"]
            )
        except UpstreamError as exc:
            logger.warning("oidc sign-in: %s", exc)
            return page("Sign-in failed", "The identity provider did not confirm the sign-in.", status_code=502)
        proof = Proof(
            method="oidc",
            subject=identity.sub,
            emails=(identity.email,) if identity.email_verified else (),
            label=identity.email,
            name=identity.name,
        )
        return self.login.complete(request, proof, resumed["next"], resumed["link"])

    def routes(self, prefix: str) -> list[BaseRoute]:
        from mindin.hosted.app import limited

        return [
            Route(prefix + "/signin/oidc", endpoint=self.start, methods=["GET"]),
            Route(prefix + "/signin/oidc", endpoint=limited(self.start), methods=["POST"]),
            Route(prefix + "/oidc/callback", endpoint=self.callback, methods=["GET"]),
        ]
