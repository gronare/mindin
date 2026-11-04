from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from mindin.auth import AuthConfig
from mindin.auth.hosted_store import LOGIN_TTL, Account, HostedStore, digest
from mindin.auth.hosted_web import LOGIN_COOKIE, Browser, Limits, client_address, error_box, esc, page
from mindin.auth.identities import Identities, Outcome, Proof
from mindin.auth.pages import stamp


class SignInMethod(Protocol):
    method: str

    def button(self, next_url: str) -> str: ...

    def connect_form(self, csrf: str) -> str: ...

    def routes(self, prefix: str) -> list[BaseRoute]: ...


def hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'


class Login:
    def __init__(self, config: AuthConfig, store: HostedStore, browser: Browser, limits: Limits) -> None:
        self.config = config
        self.store = store
        self.identities = Identities(store)
        self.browser = browser
        self.limits = limits
        self.methods: list[SignInMethod] = []
        self.ambient: Callable[[Request, str], Any] | None = None
        browser.sections.append(self.account_section)

    def add(self, method: SignInMethod) -> None:
        self.methods.append(method)

    def local(self, path: str) -> str:
        return self.browser.local(path)

    def next_of(self, raw: object) -> str:
        return self.browser.safe_return(str(raw or ""))

    async def page(self, request: Request) -> Response:
        next_url = self.next_of(request.query_params.get("next"))
        session = self.browser.signed_in(request)
        if session is not None and session[0].active:
            return RedirectResponse(next_url, status_code=303)
        if self.ambient is not None:
            answered = await self.ambient(request, next_url)
            if answered is not None:
                return answered
        return self.render(next_url)

    def render(self, next_url: str, notice: str = "", status_code: int = 200) -> Response:
        buttons = "".join(method.button(next_url) for method in self.methods)
        return page("Sign in", "Choose how to sign in to your vault.", notice + buttons, status_code=status_code)

    def begin(self, kind: str, document: dict[str, Any], target: str, state: str) -> Response:
        binding = secrets.token_urlsafe(32)
        self.identities.begin(kind, state, binding, document, LOGIN_TTL)
        response = RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})
        self.browser.set_cookie(response, LOGIN_COOKIE, binding, LOGIN_TTL)
        return response

    def resume(self, request: Request, kind: str) -> dict[str, Any] | Response:
        taken = self.identities.take(kind, request.query_params.get("state", ""))
        binding = request.cookies.get(LOGIN_COOKIE, "")
        if taken is None or not binding or not hmac.compare_digest(taken[1], digest(binding)):
            return page("Sign-in expired", "Start again from the page you came from.", status_code=400)
        if not self.limits.callback.allow(client_address(request)):
            return page("Slow down", "Too many sign-ins from here. Try again in a minute.", status_code=429)
        return taken[0]

    def linking(self, request: Request, form: dict[str, str]) -> str | Response:
        session = self.browser.signed_in(request)
        if session is None or not session[0].active or not self.browser.csrf_ok(form.get("csrf"), session[1]):
            return page("Not signed in", "Open your account page again.", status_code=403)
        return session[0].id

    def complete(self, request: Request, proof: Proof, next_url: str, link_account: str = "") -> Response:
        if link_account:
            return self.attach(request, proof, link_account)
        outcome = self.identities.sign_in(proof, self.config.registration)
        if outcome.account is None or not outcome.account.active:
            return self.refused(proof, outcome)
        response = RedirectResponse(self.next_of(next_url), status_code=303, headers={"Cache-Control": "no-store"})
        return self.browser.start_session(request, response, outcome.account)

    def attach(self, request: Request, proof: Proof, account_id: str) -> Response:
        session = self.browser.signed_in(request)
        if session is None or session[0].id != account_id or not session[0].active:
            return page("Not signed in", "Open your account page again.", status_code=403)
        if not self.identities.link(account_id, proof):
            return page("Already in use", "That sign-in already opens another account.", status_code=409)
        return RedirectResponse(self.local("/account/settings"), status_code=303, headers={"Cache-Control": "no-store"})

    def refused(self, proof: Proof, outcome: Outcome) -> Response:
        if outcome.account is not None:
            return self.browser.no_access(outcome.account.email, suspended=True)
        if outcome.refusal == "unverified":
            return self.browser.no_access("", verified=False)
        return self.browser.no_access(proof.emails[0] if proof.emails else proof.label)

    def account_section(self, account: Account, csrf: str) -> str:
        ways = self.identities.ways_in(account.id)
        removable = len(ways) > 1
        rows = "".join(
            f"<tr><td>{esc(way.method_name)}</td><td>{esc(way.label)}</td>"
            f'<td class="nowrap">{esc(stamp(way.last_used_at or way.created_at))}</td><td>'
            + (
                f'<form method="post" action="{esc(self.local("/account/identities/remove"))}">'
                f"{hidden('csrf', csrf)}{hidden('method', way.method)}{hidden('subject', way.subject)}"
                f'<button type="submit" class="btn secondary small" aria-label="Remove {esc(way.method_name)}'
                f' {esc(way.label)}">Remove</button></form>'
                if removable
                else ""
            )
            + "</td></tr>"
            for way in ways
        )
        table = (
            '<div class="tablewrap"><table><thead><tr><th>Method</th><th>Detail</th><th>Last used</th><th></th></tr>'
            f"</thead><tbody>{rows}</tbody></table></div>"
            if rows
            else '<p class="small muted">No ways in yet.</p>'
        )
        adding = "".join(method.connect_form(csrf) for method in self.methods)
        return f'<div class="stack"><h2>Ways in</h2>{table}<div class="adding">{adding}</div></div>'

    async def remove(self, request: Request) -> Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        account, csrf, form = submitted
        removal = self.identities.remove(account.id, form.get("method", ""), form.get("subject", ""))
        if removal == "last":
            notice = error_box("That is your last way in. Add another before removing it.")
            return self.browser.render_settings(account, csrf, notice, status_code=400)
        return RedirectResponse(self.local("/account/settings"), status_code=303)

    def routes(self, prefix: str) -> list[BaseRoute]:
        from mindin.auth.hosted import limited

        routes: list[BaseRoute] = [
            Route(prefix + "/signin", endpoint=self.page, methods=["GET"]),
            Route(prefix + "/account/identities/remove", endpoint=limited(self.remove), methods=["POST"]),
        ]
        for method in self.methods:
            routes += method.routes(prefix)
        return routes


def next_field(next_url: str) -> str:
    return hidden("next", next_url)
