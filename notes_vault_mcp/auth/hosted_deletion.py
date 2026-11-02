from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from notes_vault_mcp.auth.hosted_keys import SealedVaults, delete_account
from notes_vault_mcp.auth.hosted_store import Account
from notes_vault_mcp.auth.hosted_web import SESSION_COOKIE, Browser, error_box, esc, hidden_field, page

if TYPE_CHECKING:
    from notes_vault_mcp.vault import SubjectVaults


class SelfDeletion:
    def __init__(self, browser: Browser, vaults: SubjectVaults, sealed: SealedVaults) -> None:
        self.browser = browser
        self.vaults = vaults
        self.sealed = sealed

    def section(self, account: Account, csrf: str) -> str:
        target = esc(self.browser.local("/account/delete"))
        return (
            '<div class="danger-zone"><h2>Delete account</h2><p class="small muted">Deletes every vault you own.'
            " A shared vault with other members has to be handed over or deleted first; the vaults you are a member"
            " of keep your past changes under your email.</p>"
            f'<div><a class="btn danger" href="{target}">Delete account…</a></div></div>'
        )

    def still_shared(self, account: Account) -> Response | None:
        kept = self.browser.store.shared_with_others(account.id)
        if not kept:
            return None
        links = "".join(
            f'<li><a href="{esc(self.browser.local(f"/account/vaults/{vault.slug}"))}">{esc(vault.name)}</a></li>'
            for vault in kept
        )
        body = (
            f"<ul>{links}</ul><p>Hand each of them over to another member, or delete it for everyone, on its page."
            " Then delete your account.</p>"
        )
        lead = "You own shared vaults that other members still use."
        return page("Hand over first", lead, body, status_code=409)

    def destroyed(self, account: Account) -> str:
        vaults = "".join(
            f"<li>{esc(vault.name)}: {self.files_of(vault.id)} files, and the key they are encrypted with.</li>"
            for vault in self.browser.store.vaults(account.id)
        )
        keys = len(self.browser.store.keys(account.id))
        return (
            f"<ul>{vaults}"
            "<li>Each vault's search index.</li>"
            f"<li>Your {keys} keys, every way you sign in, every session and every connected client.</li>"
            "<li>The support access log.</li>"
            "</ul><p>The keys go first, so nothing stored can be read again.</p>"
        )

    def files_of(self, vault_id: str) -> int:
        return sum(1 for entry in self.vaults.for_subject(vault_id).backend.list_all() if not entry.is_dir)

    def confirm_page(self, account: Account, csrf: str, error: str = "", status_code: int = 200) -> Response:
        vaults = esc(self.browser.local("/account"))
        body = f"""{error_box(error)}<p>This destroys, at once and for good:</p>
{self.destroyed(account)}
<p>Keep a copy first: each vault on <a href="{vaults}">your vaults</a> downloads as a zip.</p>
<form method="post" action="{esc(self.browser.local("/account/delete"))}">
<input type="hidden" name="csrf" value="{esc(csrf)}">
<label>Type {esc(account.email)} to confirm
<input type="email" name="email" autocomplete="off" required>
</label>
<button type="submit">Delete my account and vaults</button>
</form>"""
        return page("Delete your account", f"Signed in as {account.email}.", body, status_code=status_code)

    def sign_in_again(self, csrf: str, status_code: int = 200, then: str = "", what: str = "the account") -> Response:
        body = f"""<form method="post" action="{esc(self.browser.local("/account/delete/again"))}">
<input type="hidden" name="csrf" value="{esc(csrf)}">
{hidden_field("next", then or self.browser.local("/account/delete"))}
<button type="submit">Sign in again</button>
</form>"""
        lead = f"Deleting {what} needs a sign-in from the last ten minutes."
        return page("Sign in again", lead, body, status_code=status_code)

    async def show(self, request: Request) -> Response:
        session = await self.browser.require_session(request, self.browser.local("/account/delete"))
        if isinstance(session, Response):
            return session
        account, csrf = session
        if not self.browser.recent(request):
            return self.sign_in_again(csrf)
        refused = self.still_shared(account)
        if refused is not None:
            return refused
        return await anyio.to_thread.run_sync(self.confirm_page, account, csrf)

    async def again(self, request: Request) -> Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        self.browser.store.end_session(request.cookies.get(SESSION_COOKIE, ""))
        then = self.browser.safe_return(submitted[2].get("next", "") or self.browser.local("/account/delete"))
        response = await self.browser.start_sign_in(request, then)
        response.delete_cookie(SESSION_COOKIE, path=self.browser.cookie_path)
        return response

    async def delete(self, request: Request) -> Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        account, csrf, form = submitted
        if not self.browser.recent(request):
            return self.sign_in_again(csrf, status_code=403)
        refused = self.still_shared(account)
        if refused is not None:
            return refused
        if form.get("email", "").strip().lower() != account.email.lower():
            error = "The address you typed is not this account's."
            return await anyio.to_thread.run_sync(lambda: self.confirm_page(account, csrf, error, 400))
        await anyio.to_thread.run_sync(self.erase, account)
        response = page("Account deleted", f"{account.email} and its vaults are gone.")
        response.delete_cookie(SESSION_COOKIE, path=self.browser.cookie_path)
        return response

    def erase(self, account: Account) -> None:
        for vault in self.browser.store.vaults(account.id):
            self.vaults.discard(vault.id)
        delete_account(self.sealed, account, self.vaults.cache)

    def routes(self, prefix: str, limited: Callable) -> list[Route]:
        return [
            Route(prefix + "/account/delete", endpoint=self.show, methods=["GET"]),
            Route(prefix + "/account/delete", endpoint=limited(self.delete), methods=["POST"]),
            Route(prefix + "/account/delete/again", endpoint=limited(self.again), methods=["POST"]),
        ]
