from __future__ import annotations

import logging
import re
import smtplib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from mindin.auth import mail
from mindin.auth.hosted_keys import StillShared
from mindin.auth.hosted_orgs import (
    ADMIN,
    ALLOWED,
    DENIED,
    EXPORT_AND_DELETE,
    KEEP,
    Departure,
    Organisation,
    OrganisationError,
    Organisations,
    OrgDomain,
    OrgVault,
    domain_of,
)
from mindin.auth.hosted_store import Account, MembershipError, VaultNameError
from mindin.auth.hosted_vault_pages import VaultPages, counted
from mindin.auth.hosted_vaults import NAME_MAX, SLUG_MAX
from mindin.auth.hosted_web import copy_field, error_box, esc, hidden_field
from mindin.auth.login_email import Mailer
from mindin.auth.pages import stamp
from mindin.extensions import Periodic

logger = logging.getLogger(__name__)

DEPARTURES_EVERY = 3600.0
DAYS_RE = re.compile(r"[0-9]{1,3}")
SOURCES = {"domain": "email domain", "admin": "added by an admin", "created": "made the organisation"}


def day_label(moment: int, now: float) -> str:
    day, today = datetime.fromtimestamp(moment, UTC).date(), datetime.fromtimestamp(now, UTC).date()
    if day == today:
        return "today " + stamp(moment)[11:]
    if day == today - timedelta(days=1):
        return "yesterday"
    return stamp(moment)[:10]


def long_date(moment: int) -> str:
    day = datetime.fromtimestamp(moment, UTC)
    return f"{day.day} {day:%b %Y}"


def joined_through(count: int) -> str:
    if count == 0:
        return "nobody has joined through it yet"
    return f"{count} {'person' if count == 1 else 'people'} joined through it"


def check_result(item: OrgDomain, now: float) -> str:
    if item.checked_at is None:
        return "Not checked yet."
    found = "no TXT record yet" if not item.checked_records else "a TXT record is there, but not this value"
    return f"Checked {day_label(item.checked_at, now)}: {found}. DNS changes can take up to an hour."


def notice_box(text: str) -> str:
    return f'<div class="notice"><p>{esc(text)}</p></div>'


def departure_mail(public_url: str, departure: Departure) -> mail.Mail:
    days = departure.organisation.removal_days
    link = f"{public_url.rstrip('/')}/account"
    return mail.branded(
        f"{departure.organisation.name} removed you from its vaults",
        "Download your vaults",
        f"{departure.organisation.name} removed {departure.account.email} from its vaults on "
        f"{mail.host_of(public_url)}. Your own vaults and your account are deleted in {days} days. Sign in to "
        "download each of your vaults as a zip before then.",
        "Download your vaults",
        link,
        "Each vault's page has a Download zip button.",
    )


def delete_departed(organisations: Organisations, erase: Callable[[Account], None]) -> int:
    deleted = 0
    for organisation_id, account_id in organisations.due():
        account = organisations.store.account_by_id(account_id)
        if account is not None and not organisations.of_account(account.id):
            try:
                erase(account)
            except StillShared as exc:
                logger.warning("organisation departure: %s", exc)
                continue
            deleted += 1
        organisations.settle(organisation_id, account_id)
    return deleted


class OrganisationPages:
    def __init__(self, pages: VaultPages) -> None:
        self.pages = pages
        self.browser = pages.browser
        self.store = pages.store
        self.organisations = Organisations(self.store, self.browser.clock)
        smtp = self.browser.config.smtp
        self.mailer = Mailer(smtp, pages.smtp_factory) if smtp else None
        pages.shared.organisations = self.organisations

    def local(self, path: str) -> str:
        return self.browser.local(path)

    def org_url(self, organisation: Organisation, path: str = "") -> str:
        return self.local(f"/account/organisation/{organisation.id}{path}")

    def form(self, csrf: str, organisation: Organisation, path: str, extra: str = ' class="row"') -> str:
        return self.pages.form_head(csrf, self.org_url(organisation, path), extra)

    def section(self, account: Account, csrf: str) -> str:
        belongs = self.organisations.of_account(account.id)
        if not belongs:
            return ""
        rows = "".join(
            f"<tr><td>{esc(organisation.name)}</td><td>{'Admin' if role == ADMIN else 'Member'}</td>"
            f"<td>{self.manage_link(organisation) if role == ADMIN else ''}</td></tr>"
            for organisation, role in belongs
        )
        return (
            '<div class="stack"><h2>Organisation</h2><div class="tablewrap"><table><thead><tr><th>Name</th>'
            f"<th>Role</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>"
            '<p class="small muted">Your own vaults, keys and connections stay yours. The organisation\'s vaults show'
            " under Vaults.</p></div>"
        )

    def manage_link(self, organisation: Organisation) -> str:
        return f'<a class="btn secondary small" href="{esc(self.org_url(organisation))}">Manage</a>'

    def remove_button(
        self,
        csrf: str,
        organisation: Organisation,
        path: str,
        field: str,
        value: str,
        label: str,
        vault: str = "",
        kind: str = " secondary",
    ) -> str:
        return (
            f"{self.form(csrf, organisation, path, '')}{hidden_field(field, value)}"
            f"{hidden_field('vault', vault) if vault else ''}"
            f'<button type="submit" class="btn{kind} small" aria-label="{esc(label)} {esc(value)}">'
            f"{esc(label)}</button></form>"
        )

    def domains_section(self, organisation: Organisation, csrf: str) -> str:
        items = sorted(self.organisations.domains(organisation), key=lambda item: (not item.verified, item.domain))
        rows = "".join(self.domain_row(organisation, csrf, item, at) for at, item in enumerate(items))
        listing = (
            f'<div class="vaults">{rows}</div>'
            if rows
            else '<p class="small muted">No domain yet: only the people you add below are members.</p>'
        )
        return f"""<div class="stack"><h2>Email domains</h2>
<p class="small muted">Once a domain is verified, whoever signs in with a verified address at it joins the
organisation, with no invitation. The domain must match exactly: add each subdomain on its own. Public email
providers such as gmail.com cannot be added.</p>{listing}
{self.form(csrf, organisation, "/domains")}
<label class="field">Add a domain<input type="text" name="domain" required maxlength="253"
placeholder="example.com" autocomplete="off"></label>
<button class="btn" type="submit">Add domain</button></form></div>"""

    def domain_row(self, organisation: Organisation, csrf: str, item: OrgDomain, at: int) -> str:
        remove = self.remove_button(csrf, organisation, "/domains/remove", "domain", item.domain, "Remove")
        name = f"<b><code>@{esc(item.domain)}</code></b>"
        if item.verified and item.verified_at is not None:
            count = sum(
                1
                for member in self.organisations.members(organisation)
                if member.source == "domain" and domain_of(member.account.email) == item.domain
            )
            facts = f"{joined_through(count)} · the record is not read again"
            return (
                f'<div class="domain"><div class="head"><div>{name} <span class="chip">Verified'
                f' {long_date(item.verified_at)}</span><p class="facts">{esc(facts)}</p></div>{remove}</div></div>'
            )
        check = self.remove_button(csrf, organisation, "/domains/check", "domain", item.domain, "Check now", kind="")
        facts = "Nobody joins through it until the record below is found. " + check_result(item, self.browser.clock())
        return (
            f'<div class="domain"><div class="head"><div>{name} <span class="chip warn">Not verified</span>'
            f'<p class="facts">{esc(facts)}</p></div><div class="row">{check}{remove}</div></div>'
            f'<div class="record"><span class="lab">Host</span>{copy_field(f"record-host-{at}", item.record_name)}'
            '<span class="lab">Type</span><div><code>TXT</code></div>'
            f'<span class="lab">Value</span>{copy_field(f"record-value-{at}", item.record_value)}</div></div>'
        )

    def people_section(self, account: Account, organisation: Organisation, csrf: str) -> str:
        members = self.organisations.members(organisation)
        rows = []
        for member in members:
            email = member.account.email
            if member.admin:
                role = "Admin"
                change = self.remove_button(csrf, organisation, "/admins/remove", "email", email, "Remove admin")
            else:
                role = "Member"
                change = self.remove_button(csrf, organisation, "/admins", "email", email, "Make admin")
            remove = (
                ""
                if member.account.id == account.id
                else self.remove_button(csrf, organisation, "/members/remove", "email", email, "Remove")
            )
            rows.append(
                f"<tr><td>{esc(email)}</td><td>{role}</td><td>{esc(SOURCES.get(member.source, member.source))}</td>"
                f'<td class="nowrap">{esc(stamp(member.joined_at)[:10])}</td>'
                f'<td><div class="row">{change}{remove}</div></td></tr>'
            )
        return f"""<div class="stack"><h2>Members</h2>
<div class="tablewrap"><table><thead><tr><th>Member</th><th>Role</th><th>Joined by</th><th>Since</th><th></th></tr>
</thead><tbody>{"".join(rows)}</tbody></table></div>
{self.form(csrf, organisation, "/members")}
<label class="field">Add by email<input type="email" name="email" required maxlength="254"
placeholder="name@example.com" autocomplete="off"></label>
<button class="btn" type="submit">Add member</button></form>
<p class="small muted">Admins manage domains, members and the organisation's vaults; there is always at least one.
Every member signs in with their own account and makes their own keys.</p></div>"""

    def vault_rows(self, organisation: Organisation, csrf: str, items: list[OrgVault]) -> str:
        rows = []
        for item in items:
            vault = item.vault
            editors = "".join(
                f'<li class="row">{esc(editor.email)} '
                + self.remove_button(
                    csrf, organisation, "/vaults/editors/remove", "email", editor.email, "Remove", vault.slug
                )
                + "</li>"
                for editor in item.editors
            )
            state = "Locked: only editors write" if item.locked else "Every member reads and writes"
            toggle = (
                f"{self.form(csrf, organisation, '/vaults/lock', '')}{hidden_field('vault', vault.slug)}"
                f"{hidden_field('locked', '' if item.locked else 'on')}"
                f'<button type="submit" class="btn secondary small">{"Unlock" if item.locked else "Lock"}</button>'
                "</form>"
            )
            adding = (
                f"{self.form(csrf, organisation, '/vaults/editors')}{hidden_field('vault', vault.slug)}"
                '<label class="field">Add an editor<input type="email" name="email" required maxlength="254"'
                ' placeholder="name@example.com" autocomplete="off"></label>'
                '<button class="btn secondary" type="submit">Add editor</button></form>'
            )
            listed = f'<ul class="plain">{editors}</ul>' if editors else '<p class="small muted">No editors.</p>'
            editing = listed + adding if item.locked else ""
            rows.append(
                f'<div class="box"><div class="head"><div><b><a href="{esc(self.pages.vault_url(vault))}">'
                f"{esc(vault.name)}</a></b> <code>{esc(vault.slug)}</code>"
                f'<p class="small muted">{esc(state)}</p></div>{toggle}</div>{editing}</div>'
            )
        return "".join(rows)

    def vaults_section(self, organisation: Organisation, csrf: str) -> str:
        items = self.organisations.vaults(organisation)
        listing = self.vault_rows(organisation, csrf, items) or '<p class="small muted">No vault yet.</p>'
        return f"""<div class="stack"><h2>Vaults</h2>
<p class="small muted">Every member reads and writes the organisation's vaults, and every change is kept with who
made it. Lock a vault to let only its editors write; everyone else still reads it.</p>
{listing}
{self.form(csrf, organisation, "/vaults")}
<label class="field">New vault<input type="text" name="name" required maxlength="{NAME_MAX}" placeholder="Wiki"></label>
<label class="field narrow">Address<input type="text" name="slug" maxlength="{SLUG_MAX}" placeholder="wiki"
pattern="[a-z][a-z0-9]*" autocomplete="off"></label>
<button class="btn" type="submit">Create vault</button></form></div>"""

    def removal_section(self, organisation: Organisation, csrf: str) -> str:
        keep = " checked" if organisation.removal == KEEP else ""
        delete = " checked" if organisation.removal == EXPORT_AND_DELETE else ""
        return f"""<div class="box"><h2>When a member is removed</h2>
<p class="small muted">Their access to the organisation's vaults stops at once, and what they wrote there stays,
under their name. Their own vaults follow this setting.</p>
{self.form(csrf, organisation, "/removal", ' class="stack"')}
<fieldset class="choice"><legend>Their own vaults</legend>
<label class="option"><input type="radio" name="removal" value="keep"{keep}><b>Keep</b>
<span>The account stays, as an account of its own.</span></label>
<label class="option"><input type="radio" name="removal" value="export-and-delete"{delete}><b>Export and delete</b>
<span>They get an email with a link to download their vaults, and the account is deleted after the days below.</span>
</label></fieldset>
<label class="field narrow">Days before deletion<input type="text" name="days" inputmode="numeric"
pattern="[0-9]{{1,3}}" value="{organisation.removal_days}" required></label>
<div><button class="btn" type="submit">Save</button></div></form></div>"""

    def sharing_section(self, organisation: Organisation, csrf: str) -> str:
        allowed = " checked" if organisation.external_sharing == ALLOWED else ""
        denied = " checked" if organisation.external_sharing == DENIED else ""
        name = esc(organisation.name)
        return f"""<div class="box"><h2>Sharing outside {name}</h2>
<p class="small muted">Whether members may invite people outside {name} to the shared vaults they own. Members can
always invite each other, and {name}'s own vaults are only ever for its members. Changing this ends no sharing that
already exists.</p>
{self.form(csrf, organisation, "/external-sharing", ' class="stack"')}
<fieldset class="choice"><legend>Members' own shared vaults</legend>
<label class="option"><input type="radio" name="external_sharing" value="denied"{denied}><b>Members only</b>
<span>An invitation to anyone outside {name} is refused.</span></label>
<label class="option"><input type="radio" name="external_sharing" value="allowed"{allowed}><b>Anyone</b>
<span>Members invite whoever they like to the vaults they own.</span></label></fieldset>
<div><button class="btn" type="submit">Save</button></div></form></div>"""

    def render(
        self, account: Account, csrf: str, organisation: Organisation, notice: str = "", status_code: int = 200
    ) -> Response:
        name = esc(organisation.name)
        members = counted(len(self.organisations.members(organisation)), "member")
        vaults = counted(len(self.organisations.vaults(organisation)), "vault")
        body = f"""<div><div class="breadcrumb"><a href="{esc(self.local("/account/settings"))}">Account</a> ›
{name}</div><h1>{name}</h1><p class="lead">Organisation · you are an admin · {members} · {vaults}</p></div>
{notice}
{self.domains_section(organisation, csrf)}
{self.people_section(account, organisation, csrf)}
{self.vaults_section(organisation, csrf)}
{self.sharing_section(organisation, csrf)}
{self.removal_section(organisation, csrf)}"""
        return self.browser.app(organisation.name, body, on="account", status_code=status_code)

    def refused(self) -> Response:
        return self.browser.app(
            "Admins only",
            '<p class="muted">Only an admin of this organisation opens this page.</p>',
            on="account",
            status_code=403,
        )

    def managed(self, account: Account, ref: str) -> Organisation | None:
        organisation = self.organisations.get(ref)
        if organisation is None or organisation.id != ref or not self.organisations.is_admin(organisation, account.id):
            return None
        return organisation

    async def page(self, request: Request) -> Response:
        ref = request.path_params["org"]
        session = await self.browser.require_session(request, self.local(f"/account/organisation/{ref}"))
        if isinstance(session, Response):
            return session
        account, csrf = session
        organisation = self.managed(account, ref)
        if organisation is None:
            return self.refused()
        return await anyio.to_thread.run_sync(self.render, account, csrf, organisation)

    def act(self, account: Account, csrf: str, organisation: Organisation, change: Callable[[], str]) -> Response:
        try:
            text = change()
        except (OrganisationError, MembershipError, VaultNameError) as exc:
            return self.render(account, csrf, organisation, error_box(str(exc)), 400)
        return self.render(account, csrf, self.organisations.require(organisation.id), notice_box(text))

    def handler(self, change: Callable[[Account, Organisation, dict], str]) -> Callable[[Request], object]:
        async def handle(request: Request) -> Response:
            submitted = await self.browser.account_form(request)
            if isinstance(submitted, Response):
                return submitted
            account, csrf, form = submitted
            organisation = self.managed(account, request.path_params["org"])
            if organisation is None:
                return self.refused()
            return await anyio.to_thread.run_sync(
                self.act, account, csrf, organisation, lambda: change(account, organisation, form)
            )

        return handle

    def add_domain(self, _: Account, organisation: Organisation, form: dict) -> str:
        item = self.organisations.add_domain(organisation, form.get("domain", ""))
        return (
            f"Add a TXT record at {item.record_name} with the value {item.record_value}, then press Check now. "
            f"Addresses at {item.domain} join {organisation.name} once it is verified."
        )

    def check_domain(self, _: Account, organisation: Organisation, form: dict) -> str:
        item = self.organisations.check_domain(organisation, form.get("domain", ""))
        return f"{item.domain} is verified: addresses there join {organisation.name} when they sign in."

    def remove_domain(self, _: Account, organisation: Organisation, form: dict) -> str:
        self.organisations.remove_domain(organisation, form.get("domain", ""))
        return "New sign-ins at that domain no longer join. Its members stay until you remove them."

    def add_member(self, _: Account, organisation: Organisation, form: dict) -> str:
        member = self.organisations.add_member(organisation, form.get("email", ""))
        return f"{member.email} is a member of {organisation.name}; they sign in at {self.public_url}/signin."

    def add_admin(self, _: Account, organisation: Organisation, form: dict) -> str:
        admin = self.organisations.add_admin(organisation, form.get("email", ""))
        return f"{admin.email} is an admin of {organisation.name}."

    def remove_admin(self, _: Account, organisation: Organisation, form: dict) -> str:
        admin = self.organisations.remove_admin(organisation, form.get("email", ""))
        return f"{admin.email} is a member, no longer an admin."

    def remove_member(self, _: Account, organisation: Organisation, form: dict) -> str:
        return self.farewell(self.organisations.remove_member(organisation, form.get("email", "")))

    def farewell(self, departure: Departure) -> str:
        email = departure.account.email
        if departure.delete_after is None:
            return f"{email} is removed; their own vaults stay with their account."
        when = stamp(departure.delete_after)[:10]
        sent = self.send_departure(departure)
        told = "An email tells them how to download their vaults." if sent else "This server sends no email; tell them."
        return f"{email} is removed; their own vaults are deleted on {when}. {told}"

    @property
    def public_url(self) -> str:
        return self.browser.config.public_url.rstrip("/")

    def send_departure(self, departure: Departure) -> bool:
        if self.mailer is None:
            return False
        subject, text, html = departure_mail(self.public_url, departure)
        try:
            self.mailer.send(departure.account.email, subject, text, html, mail.images())
        except (OSError, smtplib.SMTPException) as exc:
            logger.warning("organisation departure: sending failed: %s", exc)
            return False
        return True

    def removal(self, _: Account, organisation: Organisation, form: dict) -> str:
        raw = form.get("days", "")
        days = int(raw) if DAYS_RE.fullmatch(raw) else 0
        self.organisations.set_removal(organisation, form.get("removal", ""), days)
        return "Saved."

    def external_sharing(self, _: Account, organisation: Organisation, form: dict) -> str:
        changed = self.organisations.set_external_sharing(organisation, form.get("external_sharing", ""))
        if changed.external_sharing == ALLOWED:
            return f"Members of {changed.name} may share the vaults they own with anyone."
        return f"Members of {changed.name} share the vaults they own only with each other."

    def create_vault(self, account: Account, organisation: Organisation, form: dict) -> str:
        slug = form.get("slug", "").strip() or None
        vault = self.organisations.create_vault(organisation, account, form.get("name", ""), slug)
        self.pages.provision(vault, "")
        return f"{vault.name} is at /mcp?vault={vault.slug} for every member."

    def lock(self, _: Account, organisation: Organisation, form: dict) -> str:
        item = self.organisations.vault_named(organisation, form.get("vault", ""))
        locked = form.get("locked") == "on"
        self.organisations.lock(item, locked)
        if locked:
            return f"{item.vault.name} is locked: only its editors write, everyone else reads."
        return f"{item.vault.name} is open: every member reads and writes."

    def add_editor(self, _: Account, organisation: Organisation, form: dict) -> str:
        item = self.organisations.vault_named(organisation, form.get("vault", ""))
        editor = self.organisations.add_editor(item, form.get("email", ""))
        return f"{editor.email} edits {item.vault.name}."

    def remove_editor(self, _: Account, organisation: Organisation, form: dict) -> str:
        item = self.organisations.vault_named(organisation, form.get("vault", ""))
        self.organisations.remove_editor(item, form.get("email", ""))
        return f"{form.get('email', '').strip()} no longer edits {item.vault.name}."

    def periodic(self) -> Periodic:
        return Periodic(
            "organisation-departures",
            DEPARTURES_EVERY,
            lambda: delete_departed(self.organisations, self.pages.deletion.erase),
        )

    def routes(self, prefix: str, limited: Callable) -> list[Route]:
        base = prefix + "/account/organisation/{org}"
        posts = {
            "/domains": self.add_domain,
            "/domains/check": self.check_domain,
            "/domains/remove": self.remove_domain,
            "/members": self.add_member,
            "/members/remove": self.remove_member,
            "/admins": self.add_admin,
            "/admins/remove": self.remove_admin,
            "/removal": self.removal,
            "/external-sharing": self.external_sharing,
            "/vaults": self.create_vault,
            "/vaults/lock": self.lock,
            "/vaults/editors": self.add_editor,
            "/vaults/editors/remove": self.remove_editor,
        }
        return [
            Route(base, endpoint=self.page, methods=["GET"]),
            *(
                Route(base + path, endpoint=limited(self.handler(change)), methods=["POST"])
                for path, change in posts.items()
            ),
        ]
