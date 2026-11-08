from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from mindin.auth.hosted_history import PAGE_ROWS, Change, History, NothingToRestore, restore_change
from mindin.auth.hosted_keys import VaultFacts
from mindin.auth.hosted_members import Invitations
from mindin.auth.hosted_store import Account, Member, MembershipError
from mindin.auth.hosted_vault_pages import VaultPages, ago, counted
from mindin.auth.hosted_vaults import NAME_MAX, Vault
from mindin.auth.html import button, error_box, esc, form_head, hidden_field, muted, notice_box, table
from mindin.shared import WHAT_BELONGS, Actor

if TYPE_CHECKING:
    from mindin.auth.hosted_orgs import Organisations, OrgVault

RECENT_ROWS = 5
WEB_CLIENT = "the vault's web page"


class SharedPages:
    def __init__(self, pages: VaultPages) -> None:
        self.pages = pages
        self.store = pages.store
        self.browser = pages.browser
        self.invitations = Invitations(pages.browser.config, pages.store, pages.smtp_factory)
        self.organisations: Organisations | None = None

    def org_vault(self, vault: Vault) -> OrgVault | None:
        return self.organisations.org_vault(vault) if self.organisations is not None else None

    def org_section(self, account: Account, vault: Vault, item: OrgVault, members: list[Member]) -> str:
        assert self.organisations is not None
        table = self.member_rows(account, vault, "", [member for member in members if not member.invited], False)
        manage = (
            f' <a href="{esc(self.pages.local(f"/account/organisation/{item.organisation.id}"))}">Manage</a>'
            if self.organisations.is_admin(item.organisation, account.id)
            else ""
        )
        editors = ", ".join(editor.email for editor in item.editors) or "none yet"
        locked = (
            f'<p class="small muted">Locked: only its editors write ({esc(editors)}); every other member reads.</p>'
            if item.locked
            else ""
        )
        return f"""<div class="stack"><h2>Members</h2>{locked}{table}
<p class="small muted">Every member of {esc(item.organisation.name)} reaches this vault; its admins manage who that
is.{manage}</p></div>"""

    def history(self, vault: Vault) -> History:
        return self.pages.sealed.history(vault.id)

    def who(self, account: Account, change: Change) -> str:
        return "you" if change.author == account.id else change.author_email

    def summary_line(self, account: Account, vault: Vault, facts: VaultFacts) -> str:
        last = self.history(vault).last_change()
        members = 1 + self.store.others_in(vault)
        changed = (
            f"last change {ago(last.at, self.browser.clock())} by {self.who(account, last)}"
            if last
            else "no changes yet"
        )
        return " · ".join([counted(facts.notes, "note"), counted(members, "member"), changed])

    def change_url(self, vault: Vault, change: Change, path: str = "") -> str:
        return self.pages.vault_url(vault, f"/changes/{change.id}{path}")

    def change_item(self, account: Account, vault: Vault, change: Change) -> str:
        action = "restored" if change.restored_from else change.action
        moved = f" from {change.from_path}" if change.action == "moved" and change.from_path else ""
        links = f'<a href="{esc(self.change_url(vault, change))}">see change</a>'
        if change.restorable:
            links += f' · <a href="{esc(self.change_url(vault, change))}#restore">restore previous</a>'
        return (
            f'<li><span class="what">{esc(change.path)}</span>'
            f'<span class="when">{esc(ago(change.at, self.browser.clock()))}</span>'
            f'<span class="who">{esc(action)}{esc(moved)} by {esc(self.who(account, change))} · '
            f"{esc(change.client)} · {links}</span></li>"
        )

    def change_list(self, account: Account, vault: Vault, changes: list[Change]) -> str:
        if not changes:
            return muted("No changes yet. Every write, move and delete shows here.")
        items = "".join(self.change_item(account, vault, change) for change in changes)
        return f'<ul class="plain history">{items}</ul>'

    def member_rows(self, account: Account, vault: Vault, csrf: str, members: list[Member], manage: bool = True) -> str:
        owner = manage and vault.account_id == account.id
        now = self.browser.clock()
        rows = []
        for member in members:
            if member.invited and not owner:
                continue
            role = '<span class="chip role">Invited</span>' if member.invited else member.role.capitalize()
            last = ago(member.last_change_at, now) if member.last_change_at else "–"
            action = ""
            if owner and member.invited:
                action = (
                    f"{form_head(csrf, self.pages.vault_url(vault, '/members/withdraw'))}"
                    f"{hidden_field('email', member.email)}"
                    f"{button('Withdraw', 'btn secondary small', f'Withdraw {member.email}')}</form>"
                )
            elif owner and member.role == "member" and member.account_id:
                action = (
                    f"{form_head(csrf, self.pages.vault_url(vault, '/members/remove'))}"
                    f"{hidden_field('account', member.account_id)}"
                    f"{button('Remove', 'btn secondary small', f'Remove {member.email}')}</form>"
                )
            cells = f'<td>{esc(member.email)}</td><td>{role}</td><td class="nowrap">{esc(last)}</td>'
            rows.append(f"<tr>{cells}<td>{action}</td></tr>" if owner else f"<tr>{cells}</tr>")
        return table(["Member", "Role", "Last change", *([""] if owner else [])], rows)

    def members_section(self, account: Account, vault: Vault, csrf: str, members: list[Member]) -> str:
        table = self.member_rows(account, vault, csrf, members)
        if vault.account_id != account.id:
            return f'<div class="stack"><h2>Members</h2>{table}</div>'
        return f"""<div class="stack"><h2>Members</h2>{table}
{form_head(csrf, self.pages.vault_url(vault, "/members/invite"), ' class="row"')}
<label class="field">Invite by email
<input type="email" name="email" required maxlength="254" placeholder="name@example.com" autocomplete="off"></label>
<button class="btn" type="submit">Send invitation</button></form>
<p class="small muted">Members sign in with their own account and make their own keys and connections. Their access
ends the moment you remove them.</p></div>"""

    def ending_section(self, account: Account, vault: Vault, csrf: str, members: list[Member]) -> str:
        name = esc(vault.name)
        if vault.account_id != account.id:
            return f"""<div class="danger-zone"><h2>Leave this vault</h2>
<p class="small muted">Your keys and connections to {name} stop at once. Your past changes stay in its history.</p>
<details><summary class="btn danger">Leave {name}…</summary>
{form_head(csrf, self.pages.vault_url(vault, "/leave"))}
<button class="btn danger" type="submit">Leave this vault</button></form></details></div>"""
        others = [member for member in members if member.role == "member" and member.account_id]
        if others:
            options = "".join(
                f'<option value="{esc(member.account_id)}">{esc(member.email)}</option>' for member in others
            )
            hand_over = f"""<details><summary class="btn secondary">Make owner…</summary>
{form_head(csrf, self.pages.vault_url(vault, "/hand-over"), ' class="stack"')}
<label class="field">The new owner<select name="account">{options}</select></label>
<p class="small muted">You stay a member and can still read and write.</p>
<div><button class="btn" type="submit">Make owner</button></div></form></details>"""
        else:
            hand_over = '<p class="small muted">Invite a member first to hand the vault over.</p>'
        return f"""<div class="danger-zone"><h2>Hand over or delete</h2>
<p class="small muted">Make another member the owner, or delete the vault for everyone.</p>
<div class="row">{hand_over}
<details><summary class="btn danger">Delete {name}…</summary>
{form_head(csrf, self.pages.vault_url(vault, "/delete"), ' class="stack"')}
<label class="field">Type {name} to confirm
<input type="text" name="name" autocomplete="off" required maxlength="{NAME_MAX}"></label>
<p class="small muted">Its notes, history, members, keys and connections go, for everyone.</p>
<div><button class="btn danger" type="submit">Delete this vault for good</button></div></form></details>
</div></div>"""

    def org_ending(self, account: Account, vault: Vault, csrf: str) -> str:
        if vault.account_id != account.id:
            return ""
        name = esc(vault.name)
        return f"""<div class="danger-zone"><h2>Delete</h2>
<p class="small muted">Deletes the vault for every member of the organisation.</p>
<details><summary class="btn danger">Delete {name}…</summary>
{form_head(csrf, self.pages.vault_url(vault, "/delete"), ' class="stack"')}
<label class="field">Type {name} to confirm
<input type="text" name="name" autocomplete="off" required maxlength="{NAME_MAX}"></label>
<p class="small muted">Its notes, history, keys and connections go, for everyone.</p>
<div><button class="btn danger" type="submit">Delete this vault for good</button></div></form></details></div>"""

    def render(self, account: Account, csrf: str, vault: Vault, notice: str = "", status_code: int = 200) -> Response:
        facts = self.pages.facts(vault)
        owner = self.store.account_by_id(vault.account_id)
        whose = "you own it" if vault.account_id == account.id else f"owned by {owner.email if owner else 'nobody'}"
        item = self.org_vault(vault)
        if item is not None:
            whose = item.organisation.name
        lead = " · ".join(["Shared", "docs only", counted(facts.notes, "note"), whose])
        members = self.store.members(vault)
        people = (
            self.members_section(account, vault, csrf, members)
            if item is None
            else self.org_section(account, vault, item, members)
        )
        ending = (
            self.ending_section(account, vault, csrf, members)
            if item is None
            else self.org_ending(account, vault, csrf)
        )
        changes = self.history(vault).changes(limit=RECENT_ROWS)
        name = esc(vault.name)
        body = f"""<div><div class="breadcrumb"><a href="{esc(self.pages.local("/account"))}">Vaults</a> › {name}</div>
<div class="head"><div><h1>{name}</h1><p class="lead">{esc(lead)}</p></div>
<span class="chip shared">Shared</span></div></div>
{notice}
<div class="box belongs"><b>What belongs here</b><p class="small">{esc(WHAT_BELONGS)}</p></div>
<div class="stack"><div class="head"><h2>Recent changes</h2>
<a class="small" href="{esc(self.pages.vault_url(vault, "/history"))}">Full history</a></div>
{self.change_list(account, vault, changes)}</div>
{people}
{self.pages.connect_section(account, vault, csrf, "Your connections and keys")}
{self.pages.added(account, vault, csrf)}
{ending}"""
        return self.browser.app(vault.name, body, status_code=status_code)

    async def viewing(self, request: Request, path: str) -> tuple[Account, str, Vault] | Response:
        slug = request.path_params["slug"]
        session = await self.pages.session(request, f"/account/vaults/{slug}{path}")
        if isinstance(session, Response):
            return session
        account, csrf = session
        vault = self.store.reachable_vault(account.id, slug)
        if vault is None or vault.slug != slug or not vault.shared:
            return self.pages.not_found()
        return account, csrf, vault

    def render_history(self, account: Account, vault: Vault, before: int | None) -> Response:
        changes = self.history(vault).changes(before_id=before)
        name = esc(vault.name)
        older_url = self.pages.vault_url(vault, f"/history?before={changes[-1].id}") if changes else ""
        older = f'<p><a href="{esc(older_url)}">Older changes</a></p>' if len(changes) == PAGE_ROWS else ""
        body = f"""<div><div class="breadcrumb"><a href="{esc(self.pages.local("/account"))}">Vaults</a> ›
<a href="{esc(self.pages.vault_url(vault))}">{name}</a> › History</div>
<h1>History</h1><p class="lead">Every write, move and delete in {name}, newest first, with who made it and
through what.</p></div>
<div class="stack">{self.change_list(account, vault, changes)}{older}</div>"""
        return self.browser.app(f"History · {vault.name}", body)

    async def history_page(self, request: Request) -> Response:
        found = await self.viewing(request, "/history")
        if isinstance(found, Response):
            return found
        account, _, vault = found
        raw = request.query_params.get("before", "")
        before = int(raw) if raw.isdigit() else None
        return await anyio.to_thread.run_sync(self.render_history, account, vault, before)

    def diff_block(self, history: History, change: Change) -> str:
        lines = history.diff(change)
        if not lines:
            moved = f" The note moved from {change.from_path}." if change.from_path else ""
            return f'<p class="small muted">The content did not change.{esc(moved)}</p>'
        shown = []
        for line in lines:
            if line.startswith(("+++", "---", "@@")):
                shown.append(f'<span class="hunk">{esc(line)}</span>')
            elif line.startswith("+"):
                shown.append(f'<span class="add">{esc(line)}</span>')
            elif line.startswith("-"):
                shown.append(f'<span class="del">{esc(line)}</span>')
            else:
                shown.append(esc(line) + "\n")
        return f'<pre class="diff">{"".join(shown)}</pre>'

    def render_change(self, account: Account, csrf: str, vault: Vault, change_id: int) -> Response:
        history = self.history(vault)
        change = history.change(change_id)
        if change is None:
            return self.browser.app(
                "No such change", '<p class="muted">This vault has no such change.</p>', status_code=404
            )
        name = esc(vault.name)
        action = "restored" if change.restored_from else change.action
        lead = f"{action} by {self.who(account, change)} · {change.client} · {ago(change.at, self.browser.clock())}"
        restore = ""
        if change.restorable:
            restore = f"""<div class="box" id="restore"><h2>Restore the previous version</h2>
<p class="small muted">Writes the version from before this change back to {esc(change.path)} as a new change.
Nothing in the history is lost.</p>
{form_head(csrf, self.change_url(vault, change, "/restore"))}
<button class="btn" type="submit">Restore previous</button></form></div>"""
        body = f"""<div><div class="breadcrumb"><a href="{esc(self.pages.local("/account"))}">Vaults</a> ›
<a href="{esc(self.pages.vault_url(vault))}">{name}</a> ›
<a href="{esc(self.pages.vault_url(vault, "/history"))}">History</a> › Change</div>
<h1 class="what">{esc(change.path)}</h1><p class="lead">{esc(lead)}</p></div>
{self.diff_block(history, change)}
{restore}"""
        return self.browser.app(f"Change · {vault.name}", body)

    async def change_page(self, request: Request) -> Response:
        found = await self.viewing(request, f"/changes/{request.path_params['change']}")
        if isinstance(found, Response):
            return found
        account, csrf, vault = found
        return await anyio.to_thread.run_sync(
            self.render_change, account, csrf, vault, int(request.path_params["change"])
        )

    async def submitted_shared(self, request: Request, owner: bool) -> tuple[Account, str, Vault, dict] | Response:
        found = await (self.pages.owned(request) if owner else self.pages.submitted(request))
        if isinstance(found, Response):
            return found
        if not found[2].shared:
            return self.pages.not_found()
        if self.org_vault(found[2]) is not None:
            notice = error_box("The organisation's admins decide who reaches this vault, on the organisation page.")
            return await anyio.to_thread.run_sync(lambda: self.render(found[0], found[1], found[2], notice, 409))
        return found

    async def submitted_any(self, request: Request) -> tuple[Account, str, Vault, dict] | Response:
        found = await self.pages.submitted(request)
        if isinstance(found, Response):
            return found
        if not found[2].shared:
            return self.pages.not_found()
        return found

    async def restore(self, request: Request) -> Response:
        found = await self.submitted_any(request)
        if isinstance(found, Response):
            return found
        account, csrf, vault, _ = found
        notice, status = await anyio.to_thread.run_sync(
            self.restore_change, account, vault, int(request.path_params["change"])
        )
        return await anyio.to_thread.run_sync(lambda: self.render(account, csrf, vault, notice, status))

    def restore_change(self, account: Account, vault: Vault, change_id: int) -> tuple[str, int]:
        backend = self.pages.vaults.for_subject(vault.id).backend
        actor = Actor(account.email, account.id, WEB_CLIENT)
        try:
            return notice_box(restore_change(self.history(vault), change_id, actor, backend)), 200
        except NothingToRestore as exc:
            return error_box(str(exc)), 404

    async def invite(self, request: Request) -> Response:
        found = await self.submitted_shared(request, owner=True)
        if isinstance(found, Response):
            return found
        account, csrf, vault, form = found
        try:
            invited = await anyio.to_thread.run_sync(self.invitations.invite, vault, account, form.get("email", ""))
        except MembershipError as exc:
            notice = error_box(str(exc))
            return await anyio.to_thread.run_sync(lambda: self.render(account, csrf, vault, notice, 400))
        text = (
            f"An invitation is on its way to {invited.email}."
            if invited.sent
            else self.invitations.unsent_hint(invited.email)
        )
        return await anyio.to_thread.run_sync(lambda: self.render(account, csrf, vault, notice_box(text)))

    async def withdraw(self, request: Request) -> Response:
        found = await self.submitted_shared(request, owner=True)
        if isinstance(found, Response):
            return found
        account, csrf, vault, form = found
        email = form.get("email", "").strip().lower()
        withdrawn = self.store.withdraw(vault, email)
        notice = notice_box(f"The invitation of {email} is withdrawn.") if withdrawn else ""
        return await anyio.to_thread.run_sync(lambda: self.render(account, csrf, vault, notice))

    async def remove(self, request: Request) -> Response:
        found = await self.submitted_shared(request, owner=True)
        if isinstance(found, Response):
            return found
        account, csrf, vault, form = found
        member = self.store.account_by_id(form.get("account", ""))
        try:
            removed = member is not None and self.store.remove_member(vault, member.id)
        except MembershipError as exc:
            notice = error_box(str(exc))
            return await anyio.to_thread.run_sync(lambda: self.render(account, csrf, vault, notice, 400))
        notice = (
            notice_box(f"{member.email} is removed; their keys and connections to {vault.name} no longer work.")
            if removed and member
            else ""
        )
        return await anyio.to_thread.run_sync(lambda: self.render(account, csrf, vault, notice))

    async def hand_over(self, request: Request) -> Response:
        found = await self.submitted_shared(request, owner=True)
        if isinstance(found, Response):
            return found
        account, csrf, vault, form = found
        member = self.store.account_by_id(form.get("account", ""))
        try:
            if member is None:
                raise MembershipError("Only a member of this vault can become its owner.")
            handed = self.store.hand_over(vault, member.id)
        except MembershipError as exc:
            notice = error_box(str(exc))
            return await anyio.to_thread.run_sync(lambda: self.render(account, csrf, vault, notice, 400))
        notice = notice_box(f"{member.email} owns {vault.name} now. You stay a member.")
        return await anyio.to_thread.run_sync(lambda: self.render(account, csrf, handed, notice))

    async def leave(self, request: Request) -> Response:
        found = await self.submitted_shared(request, owner=False)
        if isinstance(found, Response):
            return found
        account, csrf, vault, _ = found
        if vault.account_id == account.id:
            notice = error_box("The owner cannot leave; hand the vault over or delete it.")
            return await anyio.to_thread.run_sync(lambda: self.render(account, csrf, vault, notice, 400))
        self.store.remove_member(vault, account.id)
        notice = notice_box(f"You left {vault.name}. Your past changes stay in its history.")
        return await anyio.to_thread.run_sync(self.pages.render_list, account, notice)

    def routes(self, base: str, limited: Callable) -> list[Route]:
        return [
            Route(base + "/history", endpoint=self.history_page, methods=["GET"]),
            Route(base + "/changes/{change:int}", endpoint=self.change_page, methods=["GET"]),
            Route(base + "/changes/{change:int}/restore", endpoint=limited(self.restore), methods=["POST"]),
            Route(base + "/members/invite", endpoint=limited(self.invite), methods=["POST"]),
            Route(base + "/members/withdraw", endpoint=limited(self.withdraw), methods=["POST"]),
            Route(base + "/members/remove", endpoint=limited(self.remove), methods=["POST"]),
            Route(base + "/hand-over", endpoint=limited(self.hand_over), methods=["POST"]),
            Route(base + "/leave", endpoint=limited(self.leave), methods=["POST"]),
        ]
