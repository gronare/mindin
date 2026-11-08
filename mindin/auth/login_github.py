from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx2
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from mindin.auth import GithubConfig
from mindin.auth.hosted_web import client_address, page
from mindin.auth.html import esc, hidden_field
from mindin.auth.identities import Proof
from mindin.auth.login import Login, next_field
from mindin.auth.upstream import pkce_challenge

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API_URL = "https://api.github.com"
SCOPES = "read:user user:email"
TIMEOUT = 10.0
API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


class GithubError(Exception):
    pass


def verified_emails(listed: Any) -> tuple[str, ...]:
    if not isinstance(listed, list):
        return ()
    rows = [row for row in listed if isinstance(row, dict) and row.get("verified") is True]
    rows.sort(key=lambda row: row.get("primary") is not True)
    return tuple(str(row["email"]).strip().lower() for row in rows if isinstance(row.get("email"), str))


class GithubSignIn:
    method = "github"

    def __init__(self, login: Login, config: GithubConfig, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self.login = login
        self.config = config
        self.transport = transport

    def button(self, next_url: str) -> str:
        return (
            f'<form method="get" action="{esc(self.login.local("/signin/github"))}">{next_field(next_url)}'
            '<button type="submit">Continue with GitHub</button></form>'
        )

    def connect_form(self, csrf: str) -> str:
        return (
            f'<form method="post" action="{esc(self.login.local("/signin/github"))}">{hidden_field("csrf", csrf)}'
            '<button type="submit" class="secondary">Add GitHub</button></form>'
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
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "redirect_uri": self.login.config.github_redirect_uri,
                "scope": SCOPES,
                "state": state,
                "code_challenge": pkce_challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        document = {"verifier": verifier, "next": next_url, "link": link_account}
        return self.login.begin("github", document, f"{AUTHORIZE_URL}?{query}", state=state)

    async def callback(self, request: Request) -> Response:
        resumed = self.login.resume(request, "github")
        if isinstance(resumed, Response):
            return resumed
        if request.query_params.get("error"):
            return page("Sign-in cancelled", "GitHub did not sign you in.", status_code=400)
        try:
            proof = await self.proof(request.query_params.get("code", ""), resumed["verifier"])
        except (GithubError, httpx2.HTTPError, ValueError) as exc:
            logger.warning("github sign-in: %s", exc)
            return page("Sign-in failed", "GitHub did not confirm the sign-in.", status_code=502)
        return self.login.complete(request, proof, resumed["next"], resumed["link"])

    async def proof(self, code: str, verifier: str) -> Proof:
        async with httpx2.AsyncClient(transport=self.transport, timeout=TIMEOUT, follow_redirects=False) as http:
            answer = await http.post(
                TOKEN_URL,
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "code": code,
                    "redirect_uri": self.login.config.github_redirect_uri,
                    "code_verifier": verifier,
                },
                headers={"Accept": "application/json"},
            )
            tokens = answer.json() if answer.status_code == 200 else {}
            tokens = tokens if isinstance(tokens, dict) else {}
            access = tokens.get("access_token")
            if not isinstance(access, str) or not access:
                raise GithubError(f"GitHub refused the code ({answer.status_code}, {tokens.get('error', '')})")
            headers = {**API_HEADERS, "Authorization": f"Bearer {access}"}
            user = await http.get(f"{API_URL}/user", headers=headers)
            emails = await http.get(f"{API_URL}/user/emails", headers=headers)
        profile = user.json() if user.status_code == 200 else {}
        if not isinstance(profile, dict) or not isinstance(profile.get("id"), int):
            raise GithubError(f"GitHub sent no user ({user.status_code})")
        listed = emails.json() if emails.status_code == 200 else []
        return Proof(
            method="github",
            subject=str(profile["id"]),
            emails=verified_emails(listed),
            label=str(profile.get("login") or profile["id"]),
            name=str(profile.get("name") or ""),
        )

    def routes(self, prefix: str) -> list[BaseRoute]:
        from mindin.auth.hosted import limited

        return [
            Route(prefix + "/signin/github", endpoint=self.start, methods=["GET"]),
            Route(prefix + "/signin/github", endpoint=limited(self.start), methods=["POST"]),
            Route(prefix + "/signin/github/callback", endpoint=self.callback, methods=["GET"]),
        ]
