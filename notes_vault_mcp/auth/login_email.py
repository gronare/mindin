from __future__ import annotations

import logging
import re
import secrets
import smtplib
import ssl
import time
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from notes_vault_mcp.auth import SmtpConfig
from notes_vault_mcp.auth.hosted_web import Window, client_address, esc, page
from notes_vault_mcp.auth.identities import Proof, addresses
from notes_vault_mcp.auth.login import Login, hidden, next_field

logger = logging.getLogger(__name__)

LINK_TTL = 15 * 60
ADDRESS = re.compile(r"[^@\s<>,;\"()\[\]\\]+@[^@\s<>,;\"()\[\]\\]+\.[^@\s<>,;\"()\[\]\\.]+")
SENT = "If {email} can sign in here, a link is on its way. It works once, for 15 minutes."
SmtpFactory = Callable[..., Any]


class Mailer:
    def __init__(self, config: SmtpConfig, factory: SmtpFactory | None = None) -> None:
        self.config = config
        self.factory = factory or (smtplib.SMTP_SSL if config.security == "ssl" else smtplib.SMTP)

    def send(self, to: str, subject: str, text: str) -> None:
        message = EmailMessage()
        message["From"] = self.config.sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        context = ssl.create_default_context()
        if self.config.security == "ssl":
            client = self.factory(self.config.host, self.config.port, timeout=10, context=context)
        else:
            client = self.factory(self.config.host, self.config.port, timeout=10)
        with client as smtp:
            if self.config.security == "starttls":
                smtp.starttls(context=context)
            if self.config.username:
                smtp.login(self.config.username, self.config.password)
            smtp.send_message(message)


class EmailSignIn:
    method = "email"

    def __init__(self, login: Login, mailer: Mailer, clock: Callable[[], float] = time.monotonic) -> None:
        self.login = login
        self.mailer = mailer
        self.per_address = Window(3, LINK_TTL, clock)
        self.per_client = Window(10, LINK_TTL, clock)

    def button(self, next_url: str) -> str:
        return (
            f'<form method="post" action="{esc(self.login.local("/signin/email"))}">{next_field(next_url)}'
            '<label>Email<input type="email" name="email" autocomplete="email" required></label>'
            '<button type="submit">Email me a link</button></form>'
        )

    def connect_form(self, csrf: str) -> str:
        return ""

    def link_url(self, token: str) -> str:
        return f"{self.login.config.public_url.rstrip('/')}/signin/email/confirm?token={quote(token)}"

    def known(self, email: str) -> bool:
        if self.login.config.registration == "open":
            return True
        return self.login.identities.has_email(email) or self.login.identities.invited(email)

    async def send(self, request: Request) -> Response:
        form = await request.form()
        next_url = self.login.next_of(form.get("next"))
        found = addresses((str(form.get("email") or ""),))
        if not found or not ADDRESS.fullmatch(found[0]) or len(found[0]) > 254:
            return self.login.render(next_url, '<p class="error">Enter an email address.</p>', status_code=400)
        email = found[0]
        if not self.per_client.allow(client_address(request)) or not self.per_address.allow(email):
            return page("Slow down", "Too many links asked for. Try again in a quarter of an hour.", status_code=429)
        if self.known(email):
            token = secrets.token_urlsafe(32)
            self.login.identities.begin("email", token, "", {"email": email, "next": next_url}, LINK_TTL)
            text = (
                f"Open this link to sign in to your vault at {self.login.config.public_url}:\n\n"
                f"{self.link_url(token)}\n\nIt works once, for 15 minutes. "
                "If you did not ask for it, ignore this email.\n"
            )
            try:
                await anyio.to_thread.run_sync(self.mailer.send, email, "Your sign-in link", text)
            except (OSError, smtplib.SMTPException) as exc:
                logger.warning("email sign-in: sending failed: %s", exc)
                return page("Sending failed", "The link could not be sent. Try again later.", status_code=502)
        return page("Check your email", SENT.format(email=email))

    async def confirm_page(self, request: Request) -> Response:
        token = request.query_params.get("token", "")
        document = self.login.identities.peek("email", token)
        if document is None:
            return expired()
        body = (
            f'<form method="post" action="{esc(self.login.local("/signin/email/confirm"))}">{hidden("token", token)}'
            '<button type="submit">Sign in</button></form>'
        )
        return page("Sign in", f"Sign in as {document['email']}.", body)

    async def confirm(self, request: Request) -> Response:
        form = await request.form()
        taken = self.login.identities.take("email", str(form.get("token") or ""))
        if taken is None:
            return expired()
        document, _ = taken
        email = document["email"]
        proof = Proof(method="email", subject=email, emails=(email,), label=email)
        return self.login.complete(request, proof, document["next"])

    def routes(self, prefix: str) -> list[BaseRoute]:
        from notes_vault_mcp.auth.hosted import limited

        return [
            Route(prefix + "/signin/email", endpoint=limited(self.send), methods=["POST"]),
            Route(prefix + "/signin/email/confirm", endpoint=self.confirm_page, methods=["GET"]),
            Route(prefix + "/signin/email/confirm", endpoint=limited(self.confirm), methods=["POST"]),
        ]


def expired() -> Response:
    return page(
        "Link expired", "This sign-in link has expired or was already used. Ask for a new one.", status_code=400
    )
