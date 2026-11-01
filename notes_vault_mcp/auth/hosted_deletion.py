from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from notes_vault_mcp.auth.hosted_keys import SealedVaults, delete_account
from notes_vault_mcp.auth.hosted_store import Account, digest
from notes_vault_mcp.auth.hosted_transfer import subject_of
from notes_vault_mcp.auth.hosted_web import SESSION_COOKIE, Browser, error_box, esc, page

if TYPE_CHECKING:
    from notes_vault_mcp.vault import SubjectVaults

RECENT_SIGN_IN = 600


class SelfDeletion:
    def __init__(self, browser: Browser, vaults: SubjectVaults, sealed: SealedVaults) -> None:
        self.browser = browser
        self.vaults = vaults
        self.sealed = sealed

    def section(self, account: Account, csrf: str) -> str:
        target = esc(self.browser.local("/account/delete"))
        return (
            "<h2>Delete account</h2><p>Deleting the account destroys your vault and everything that opens it.</p>"
            f'<p><a href="{target}">Delete your account</a></p>'
        )

    def signed_in_at(self, request: Request) -> int | None:
        with self.browser.store.connect() as connection:
            row = connection.execute(
                "SELECT created_at FROM sessions WHERE id_hash = ?", (digest(request.cookies.get(SESSION_COOKIE, "")),)
            ).fetchone()
        return row["created_at"] if row else None

    def recent(self, request: Request) -> bool:
        started = self.signed_in_at(request)
        return started is not None and self.browser.clock() - started <= RECENT_SIGN_IN

    def destroyed(self, account: Account) -> str:
        files = sum(1 for entry in self.vaults.for_subject(subject_of(account)).backend.list_all() if not entry.is_dir)
        keys = len(self.browser.store.keys(account.id))
        return (
            "<ul>"
            f"<li>Your vault: {files} files, and the key they are encrypted with.</li>"
            "<li>The vault's search index.</li>"
            f"<li>Your {keys} keys, every way you sign in, every session and every connected client.</li>"
            "<li>The support access log.</li>"
            "</ul><p>The key goes first, so nothing stored can be read again.</p>"
        )

    def confirm_page(self, account: Account, csrf: str, error: str = "", status_code: int = 200) -> Response:
        export = esc(self.browser.local("/account/export"))
        body = f"""{error_box(error)}<p>This destroys, at once and for good:</p>
{self.destroyed(account)}
<p>Keep a copy first: <a href="{export}">download the whole vault as a zip</a>.</p>
<form method="post" action="{esc(self.browser.local("/account/delete"))}">
<input type="hidden" name="csrf" value="{esc(csrf)}">
<label>Type {esc(account.email)} to confirm
<input type="email" name="email" autocomplete="off" required>
</label>
<button type="submit">Delete my account and vault</button>
</form>"""
        return page("Delete your account", f"Signed in as {account.email}.", body, status_code=status_code)

    def sign_in_again(self, csrf: str, status_code: int = 200) -> Response:
        body = f"""<form method="post" action="{esc(self.browser.local("/account/delete/again"))}">
<input type="hidden" name="csrf" value="{esc(csrf)}">
<button type="submit">Sign in again</button>
</form>"""
        lead = "Deleting the account needs a sign-in from the last ten minutes."
        return page("Sign in again", lead, body, status_code=status_code)

    async def show(self, request: Request) -> Response:
        session = await self.browser.require_session(request, self.browser.local("/account/delete"))
        if isinstance(session, Response):
            return session
        account, csrf = session
        if not self.recent(request):
            return self.sign_in_again(csrf)
        return await anyio.to_thread.run_sync(self.confirm_page, account, csrf)

    async def again(self, request: Request) -> Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        self.browser.store.end_session(request.cookies.get(SESSION_COOKIE, ""))
        response = await self.browser.start_sign_in(request, self.browser.local("/account/delete"))
        response.delete_cookie(SESSION_COOKIE, path=self.browser.cookie_path)
        return response

    async def delete(self, request: Request) -> Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        account, csrf, form = submitted
        if not self.recent(request):
            return self.sign_in_again(csrf, status_code=403)
        if form.get("email", "").strip().lower() != account.email.lower():
            error = "The address you typed is not this account's."
            return await anyio.to_thread.run_sync(lambda: self.confirm_page(account, csrf, error, 400))
        await anyio.to_thread.run_sync(self.erase, account)
        response = page("Account deleted", f"{account.email} and its vault are gone.")
        response.delete_cookie(SESSION_COOKIE, path=self.browser.cookie_path)
        return response

    def erase(self, account: Account) -> None:
        self.vaults.discard(subject_of(account))
        delete_account(self.sealed, account, self.vaults.cache)

    def routes(self, prefix: str, limited: Callable) -> list[Route]:
        return [
            Route(prefix + "/account/delete", endpoint=self.show, methods=["GET"]),
            Route(prefix + "/account/delete", endpoint=limited(self.delete), methods=["POST"]),
            Route(prefix + "/account/delete/again", endpoint=limited(self.again), methods=["POST"]),
        ]
