from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import anyio
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from mindin.hosted.accounts.access import GRANT_CHOICES, SupportAccess, SupportRequest
from mindin.hosted.accounts.deletion import erase_account
from mindin.hosted.accounts.keys import SealedVaults
from mindin.hosted.accounts.store import Account
from mindin.hosted.accounts.vaults import Vault
from mindin.hosted.pages.html import box, button, error_box, esc, form_head, hidden_field, muted, table
from mindin.hosted.pages.templates import stamp
from mindin.hosted.pages.web import SESSION_COOKIE, Browser, page

if TYPE_CHECKING:
    from mindin.vault import SubjectVaults


class SupportDesk:
    def __init__(self, browser: Browser, access: SupportAccess) -> None:
        self.browser = browser
        self.access = access

    def section(self, account: Account, csrf: str) -> str:
        vaults = self.browser.store.vaults(account.id)
        granted = [(vault, until) for vault in vaults if (until := self.access.grant_until(vault.id))]
        request = self.access.pending(account.id)
        history = self.access.log(account.id)
        if request is None and not granted and not history:
            return ""
        live = "".join(
            f'<li class="row"><span>{esc(vault.name)}: open to support until {esc(stamp(until))}</span>'
            f"{form_head(csrf, self.browser.local('/account/support/end'))}{hidden_field('vault', vault.slug)}"
            f"{button('End now', 'btn secondary small')}</form></li>"
            for vault, until in granted
        )
        granted_list = f'<ul class="plain stack">{live}</ul>' if live else ""
        asked = self.request_form(request, vaults, csrf) if request else ""
        rows = "".join(
            f'<tr><td class="nowrap">{esc(stamp(row["at"]))}</td><td>{esc(row["vault_name"] or "a deleted vault")}</td>'
            f"<td>{esc(row['operator'])}</td><td>{esc(row['reason'])}</td><td>{esc(row['action'])}</td></tr>"
            for row in history
        )
        log = (
            table(["When", "Vault", "Who", "Why", "What"], [rows])
            if rows
            else muted("Nobody from support has opened your vaults.")
        )
        lead = muted(
            "Support opens a vault only when you approve its request here, for one vault and a set time, and every"
            " look is logged."
        )
        return box(f"<h2>Support access</h2>{lead}{asked}{granted_list}{log}")

    def request_form(self, request: SupportRequest, vaults: list[Vault], csrf: str) -> str:
        if request.vault is not None:
            vault_field = hidden_field("vault", request.vault.slug)
        elif len(vaults) == 1:
            vault_field = hidden_field("vault", vaults[0].slug)
        else:
            choices = "".join(f'<option value="{esc(vault.slug)}">{esc(vault.name)}</option>' for vault in vaults)
            vault_field = f'<label class="field">Vault<select name="vault">{choices}</select></label>'
        durations = "".join(
            f'<option value="{name}"{" selected" if name == "day" else ""}>{label}</option>'
            for name, (label, _) in GRANT_CHOICES.items()
        )
        head = form_head(csrf, self.browser.local("/account/support/approve"), ' class="row"')
        approve = (
            f'{head}{vault_field}<label class="field">For<select name="duration">{durations}</select></label>'
            f"{button('Approve', 'btn')}</form>"
        )
        decline = (
            f"{form_head(csrf, self.browser.local('/account/support/decline'))}"
            f"{button('Decline', 'btn secondary')}</form>"
        )
        asks = (
            f"<p><strong>{esc(request.operator)}</strong> from support asks to open {esc(request.vault_label)}:"
            f" {esc(request.reason)}</p>"
        )
        asked = muted(f"Asked {esc(stamp(request.requested_at))}.")
        return f'{asks}{asked}<div class="row">{approve}{decline}</div>'

    async def approve(self, request: Request) -> Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        account, _, form = submitted
        choice = GRANT_CHOICES.get(form.get("duration", ""))
        vault = self.browser.store.own_vault(account.id, form.get("vault", ""))
        if choice is not None and vault is not None:
            self.access.approve(account, vault, choice[1])
        return RedirectResponse(self.browser.local("/account/settings"), status_code=303)

    async def decline(self, request: Request) -> Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        self.access.decline(submitted[0].id)
        return RedirectResponse(self.browser.local("/account/settings"), status_code=303)

    async def end(self, request: Request) -> Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        account, _, form = submitted
        vault = self.browser.store.own_vault(account.id, form.get("vault", ""))
        if vault is not None:
            self.access.revoke(account.id, vault.id)
        return RedirectResponse(self.browser.local("/account/settings"), status_code=303)

    def routes(self, prefix: str, limited: Callable) -> list[Route]:
        return [
            Route(prefix + "/account/support/approve", endpoint=limited(self.approve), methods=["POST"]),
            Route(prefix + "/account/support/decline", endpoint=limited(self.decline), methods=["POST"]),
            Route(prefix + "/account/support/end", endpoint=limited(self.end), methods=["POST"]),
        ]


class SelfDeletion:
    def __init__(self, browser: Browser, vaults: SubjectVaults, sealed: SealedVaults) -> None:
        self.browser = browser
        self.vaults = vaults
        self.sealed = sealed

    def section(self, account: Account, csrf: str) -> str:
        target = esc(self.browser.local("/account/delete"))
        lead = muted(
            "Deletes every vault you own. A shared vault with other members has to be handed over or deleted first;"
            " the vaults you are a member of keep your past changes under your email."
        )
        return box(
            f'<h2>Delete account</h2>{lead}<div><a class="btn danger" href="{target}">Delete account…</a></div>',
            "danger-zone",
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
        erase_account(self.vaults, self.sealed, account)

    def routes(self, prefix: str, limited: Callable) -> list[Route]:
        return [
            Route(prefix + "/account/delete", endpoint=self.show, methods=["GET"]),
            Route(prefix + "/account/delete", endpoint=limited(self.delete), methods=["POST"]),
            Route(prefix + "/account/delete/again", endpoint=limited(self.again), methods=["POST"]),
        ]
