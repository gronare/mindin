from __future__ import annotations

import tempfile
import zipfile
from collections.abc import Callable, Iterator
from datetime import date
from typing import TYPE_CHECKING

import anyio
from mcp.server.transport_security import RequestBodyLimitMiddleware
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, request_response
from starlette.types import ASGIApp

from mindin import provision
from mindin.backends import SCHEMA_KEY, VaultBackend
from mindin.hosted.accounts.deletion import erase_vault
from mindin.hosted.accounts.import_export import (
    CHUNK_BYTES,
    MAX_UPLOAD_BYTES,
    SPOOL_BYTES,
    BadArchive,
    export_zip,
    import_zip,
)
from mindin.hosted.accounts.keys import SealedVaults, VaultFacts, facts_of
from mindin.hosted.accounts.layout import (
    BUILT_IN,
    EDITING,
    SWITCHING,
    LayoutKept,
    describe,
    layout_title,
    provision_vault,
    switch_layout,
)
from mindin.hosted.accounts.limits import LimitReached
from mindin.hosted.accounts.store import Account, VaultNameError, scope_label
from mindin.hosted.accounts.vaults import KINDS, NAME_MAX, PERSONAL, SHARED, Vault, Waiting, vault_resource_url
from mindin.hosted.config import MACHINE_KEY_PURPOSE, READ_SCOPE, WRITE_SCOPE
from mindin.hosted.pages.html import (
    ago,
    button,
    copy_field,
    counted,
    error_box,
    esc,
    form_head,
    hidden_field,
    notice_box,
    size_label,
    table,
)
from mindin.hosted.pages.shared import SharedPages
from mindin.hosted.pages.web import Browser, app_script
from mindin.hosted.signin.email import SmtpFactory
from mindin.presets import OFFERED, PRESETS
from mindin.schema import Schema, broken_schema_lines, schema_from_text

if TYPE_CHECKING:
    from mindin.hosted.pages.account import SelfDeletion
    from mindin.vault import SubjectVaults

KIND_OF_PURPOSE = {MACHINE_KEY_PURPOSE: ("Computer", "Sign out")}


class VaultPages:
    def __init__(
        self,
        browser: Browser,
        vaults: SubjectVaults,
        sealed: SealedVaults,
        deletion: SelfDeletion,
        smtp_factory: SmtpFactory | None = None,
    ) -> None:
        self.browser = browser
        self.smtp_factory = smtp_factory
        self.store = browser.store
        self.vaults = vaults
        self.sealed = sealed
        self.deletion = deletion
        self.sections: list[Callable[[Account, Vault, str], str]] = []
        self.shared = SharedPages(self)

    def local(self, path: str) -> str:
        return self.browser.local(path)

    def vault_url(self, vault: Vault, path: str = "") -> str:
        return self.local(f"/account/vaults/{vault.slug}{path}")

    def mcp_url(self, vault: Vault) -> str:
        return vault_resource_url(self.browser.config, vault.slug)

    def storage(self, vault: Vault) -> VaultBackend:
        return self.vaults.backend_factory(vault.id)[0]

    def facts(self, vault: Vault) -> VaultFacts:
        return facts_of(self.storage(vault).list_all())

    def summary_line(self, vault: Vault, facts: VaultFacts) -> str:
        keys = self.store.keys(vault.account_id, vault.id)
        computers = sum(1 for key in keys if key["purpose"] == MACHINE_KEY_PURPOSE)
        links = len(self.store.connections(vault.id, vault.account_id)) + computers
        parts = [counted(facts.notes, "note"), size_label(facts.size)]
        parts.append(f"written {ago(facts.written_at, self.browser.clock())}" if facts.written_at else "empty")
        parts.append(f"{counted(len(keys) - computers, 'key')}, {counted(links, 'connection')}")
        return " · ".join(parts)

    def vault_row(self, account: Account, vault: Vault) -> str:
        if vault.shared:
            role = "Owner" if vault.account_id == account.id else "Member"
            item = self.shared.org_vault(vault)
            label = item.organisation.name if item is not None else "Shared"
            chips = f'<span class="chip shared">{esc(label)}</span> <span class="chip role">{role}</span>'
            facts = self.shared.summary_line(account, vault, self.facts(vault))
        else:
            chips, facts = '<span class="chip">Personal</span>', self.summary_line(vault, self.facts(vault))
        return (
            f'<a class="vault" href="{esc(self.vault_url(vault))}">'
            f'<div class="name">{esc(vault.name)} {chips}</div>'
            '<span class="go" aria-hidden="true">›</span>'
            f'<div class="facts">{esc(facts)}</div>'
            f'<div class="where">…/mcp?vault={esc(vault.slug)}</div></a>'
        )

    def render_list(self, account: Account, notice: str = "", status_code: int = 200) -> Response:
        reachable = self.store.reachable_vaults(account.id)
        groups = [
            (label, "".join(self.vault_row(account, vault) for vault in reachable if vault.shared == shared))
            for label, shared in (("Personal", False), ("Shared", True))
        ]
        listing = (
            "".join(
                f'<div><p class="group-label">{label}</p><div class="vaults">{rows}</div></div>'
                for label, rows in groups
                if rows
            )
            if reachable
            else '<p class="muted">You have no vault yet. Make one to connect a client.</p>'
        )
        body = f"""<div class="head"><div><h1>Your vaults</h1><p class="lead">Each vault has its own address, keys
and connections. A connection only ever reaches the vault it was made for.</p></div>
<a class="btn" href="{esc(self.local("/account/vaults/new"))}">New vault</a></div>
{notice}{listing}"""
        return self.browser.app("Your vaults", body, status_code=status_code)

    def connection_rows(self, account: Account, vault: Vault, csrf: str) -> str:
        now = self.browser.clock()
        rows = [
            f"<tr><td>{esc(link.name)}</td><td>Connector</td><td>{esc(scope_label(link.scopes))}</td>"
            f'<td class="nowrap">{esc(ago(link.last_used_at, now))}</td><td>'
            f"{form_head(csrf, self.vault_url(vault, '/connections/disconnect'))}"
            f"{hidden_field('client', link.client_id)}"
            f"{button('Disconnect', 'btn secondary small', f'Disconnect {link.name}')}</form></td></tr>"
            for link in self.store.connections(vault.id, account.id)
        ]
        keys = sorted(self.store.keys(account.id, vault.id), key=lambda key: key["purpose"] != "device")
        for key in keys:
            kind, action = KIND_OF_PURPOSE.get(key["purpose"], ("Key", "Revoke"))
            rows.append(
                f"<tr><td>{esc(key['name'])}</td><td>{kind}</td><td>{esc(scope_label(key['scopes']))}</td>"
                f'<td class="nowrap">{esc(ago(key["last_used_at"], now))}</td><td>'
                f"{form_head(csrf, self.vault_url(vault, '/keys/revoke'))}{hidden_field('key', key['id'])}"
                f"{button(action, 'btn secondary small', action + ' ' + key['name'])}</form></td></tr>"
            )
        if not rows:
            return '<p class="small muted">Nothing reaches this vault yet.</p>'
        return table(["Name", "Kind", "Access", "Last used", ""], rows)

    def render_vault(
        self, account: Account, csrf: str, vault: Vault, notice: str = "", status_code: int = 200
    ) -> Response:
        if vault.shared:
            return self.shared.render(account, csrf, vault, notice, status_code)
        backend = self.storage(vault)
        schema = provision.stored(backend, SCHEMA_KEY)
        facts = facts_of(backend.list_all())
        lead = " · ".join(["Personal", layout_title(schema), counted(facts.notes, "note"), size_label(facts.size)])
        name = esc(vault.name)
        body = f"""<div><div class="breadcrumb"><a href="{esc(self.local("/account"))}">Vaults</a> › {name}</div>
<div class="head"><div><h1>{name}</h1><p class="lead">{esc(lead)}</p></div>
<span class="chip">Personal</span></div></div>
{notice}{broken_box(schema)}{self.layout_box(vault, csrf, schema)}
{self.connect_section(account, vault, csrf)}
{self.added(account, vault, csrf)}<div class="box"><h2>Your data</h2>
<p class="small muted">Download every file in this vault as a zip, or bring files in from one; a file with the same
path is replaced.</p>
<div class="row"><a class="btn secondary" href="{esc(self.vault_url(vault, "/export"))}">Download zip</a></div>
{form_head(csrf, self.vault_url(vault, "/import"), ' class="row" enctype="multipart/form-data"')}
<label class="field">Import a zip<input type="file" name="archive" accept=".zip,application/zip" required></label>
<button class="btn secondary" type="submit">Import</button></form></div>
<div class="danger-zone"><h2>Delete this vault</h2>
<p class="small muted">Removes its notes, keys and connections. Your other vaults stay as they are.</p>
<details><summary class="btn danger">Delete {name}…</summary>
{form_head(csrf, self.vault_url(vault, "/delete"), ' class="stack"')}
<label class="field">Type {name} to confirm
<input type="text" name="name" autocomplete="off" required maxlength="{NAME_MAX}"></label>
<div><button class="btn danger" type="submit">Delete this vault for good</button></div></form></details></div>
</div>"""
        return self.browser.app(vault.name, body, status_code=status_code)

    def added(self, account: Account, vault: Vault, csrf: str) -> str:
        return "".join(section(account, vault, csrf) for section in self.sections)

    def connect_section(self, account: Account, vault: Vault, csrf: str, heading: str = "Connections and keys") -> str:
        public = self.browser.config.public_url.rstrip("/")
        login_line = f"mindin login {public} --vault {vault.slug}"
        name = esc(vault.name)
        return f"""<div class="stack"><h2>Connect</h2><div class="cols">
<div class="box"><b>claude.ai, Claude Desktop and mobile</b>
<p class="small muted">Add a custom connector with this address. Name it after the vault, for example
<i>Vault · {name}</i>.</p>{copy_field("mcp-url", self.mcp_url(vault))}</div>
<div class="box"><b>Claude Code, Codex, Cursor</b>
<p class="small muted">Sign this computer in to the vault. A repo can point at another of your vaults with
<code>mindin vault use</code>.</p>{copy_field("cli-login", login_line)}</div>
<div class="box"><b>Obsidian (Remotely Save)</b>
<p class="small muted">WebDAV with your email as the username and a key from this vault as the password. The key
decides which vault opens.</p>{copy_field("dav-url", self.browser.config.dav_url)}</div>
</div></div>
<div class="stack"><h2>{esc(heading)}</h2>
{self.connection_rows(account, vault, csrf)}
{form_head(csrf, self.vault_url(vault, "/keys"), ' class="row"')}
<label class="field">New key, named after where it will live
<input type="text" name="name" maxlength="64" required placeholder="laptop"></label>
<label class="field narrow">Access<select name="access">
<option value="write">read and write</option><option value="read">read</option></select></label>
<button class="btn" type="submit">Make a key</button></form></div>"""

    def layout_box(self, vault: Vault, csrf: str, schema: str | None) -> str:
        current = provision.named_preset(schema)
        shown = layout_contents(schema) if schema is not None else ""
        if current == BUILT_IN:
            return f'<div class="box"><h2>Layout</h2><p class="small muted">{esc(describe(current))}</p>{shown}</div>'
        if current is None:
            heading, lead, label = (
                "Pick a layout",
                "This vault has no layout yet. Pick one to start.",
                "Use this layout",
            )
        else:
            heading, lead, label = "Layout", f"{describe(current)} {SWITCHING}", "Switch layout"
        return f"""<div class="box"><h2>{heading}</h2>
<p class="small muted">{esc(lead)}</p>{shown}<p class="small muted">{esc(EDITING)}</p>
{form_head(csrf, self.vault_url(vault, "/layout"), ' class="stack"')}
<fieldset class="choice"><legend>Layout</legend>{layout_options(current or OFFERED[0])}</fieldset>
<div><button class="btn" type="submit">{label}</button></div></form></div>"""

    async def pick_layout(self, request: Request) -> Response:
        found = await self.submitted(request)
        if isinstance(found, Response):
            return found
        account, csrf, vault, form = found
        preset = form.get("preset", "")
        if vault.shared:
            notice = error_box("A shared vault keeps its own layout; it is not switched.")
            return await anyio.to_thread.run_sync(lambda: self.render_vault(account, csrf, vault, notice, 409))
        if preset not in OFFERED:
            notice = error_box("Pick one of the layouts.")
            return await anyio.to_thread.run_sync(lambda: self.render_vault(account, csrf, vault, notice, 400))
        try:
            notice, status = notice_box(await anyio.to_thread.run_sync(switch_layout, self.vaults, vault, preset)), 200
        except LayoutKept as exc:
            notice, status = error_box(str(exc)), 409
        return await anyio.to_thread.run_sync(lambda: self.render_vault(account, csrf, vault, notice, status))

    def render_new(
        self, csrf: str, name: str = "", preset: str = "", error: str = "", status_code: int = 200, kind: str = ""
    ) -> Response:
        shared = kind == SHARED
        base = vault_resource_url(self.browser.config, "")
        body = f"""<div><div class="breadcrumb"><a href="{esc(self.local("/account"))}">Vaults</a> › New vault</div>
<h1>New vault</h1></div>
{error_box(error)}
{form_head(csrf, self.local("/account/vaults"), ' class="form"')}
<label class="field" for="vault-name">Name
<input id="vault-name" type="text" name="name" value="{esc(name)}" maxlength="{NAME_MAX}" required autofocus>
<span class="small muted">Address:
<code data-slug-from="vault-name" data-base="{esc(base)}">{esc(base)}…</code></span>
</label>
<fieldset class="choice"><legend>Kind</legend>
<label class="option"><input type="radio" name="kind" value="personal" required{"" if shared else " checked"}>
<b>Personal</b><span>Only you. Plans, logs, backlog and your own reasoning, in the layout you pick next.</span></label>
<label class="option"><input type="radio" name="kind" value="shared" required{" checked" if shared else ""}>
<b>Shared</b><span>You and the people you invite. Only how things are now: systems, how-tos, decisions and
references. Every change is kept with who made it.</span></label>
</fieldset>
<fieldset class="choice" data-layout-for-kind="personal"><legend>Layout</legend>
{layout_options(preset or OFFERED[0])}
</fieldset>
<div class="row"><button class="btn" type="submit">Create vault</button>
<a class="btn secondary" href="{esc(self.local("/account"))}">Cancel</a></div>
</form>"""
        return self.browser.app("New vault", body, status_code=status_code)

    async def session(self, request: Request, path: str) -> tuple[Account, str] | Response:
        return await self.browser.require_session(request, self.local(path))

    async def list_page(self, request: Request) -> Response:
        session = await self.session(request, "/account")
        if isinstance(session, Response):
            return session
        waiting = self.store.accept_invitations(session[0])
        return await anyio.to_thread.run_sync(self.render_list, session[0], waiting_notice(waiting))

    async def new_page(self, request: Request) -> Response:
        session = await self.session(request, "/account/vaults/new")
        if isinstance(session, Response):
            return session
        return self.render_new(session[1])

    def not_found(self) -> Response:
        return self.browser.app(
            "No such vault", '<p class="muted">You have no vault at this address.</p>', status_code=404
        )

    async def vault_page(self, request: Request) -> Response:
        slug = request.path_params["slug"]
        session = await self.session(request, f"/account/vaults/{slug}")
        if isinstance(session, Response):
            return session
        account, csrf = session
        self.store.accept_invitations(account)
        vault = self.store.reachable_vault(account.id, slug)
        if vault is None or vault.slug != slug:
            return self.not_found()
        return await anyio.to_thread.run_sync(self.render_vault, account, csrf, vault)

    async def submitted(self, request: Request) -> tuple[Account, str, Vault, dict] | Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        account, csrf, form = submitted
        vault = self.store.reachable_vault(account.id, request.path_params["slug"])
        if vault is None or vault.slug != request.path_params["slug"]:
            return self.not_found()
        return account, csrf, vault, form

    async def owned(self, request: Request) -> tuple[Account, str, Vault, dict] | Response:
        found = await self.submitted(request)
        if isinstance(found, Response) or found[2].account_id == found[0].id:
            return found
        return self.browser.app(
            "Owner only", '<p class="muted">Only the owner of this vault can do that.</p>', status_code=403
        )

    async def create(self, request: Request) -> Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        account, csrf, form = submitted
        name, preset, kind = form.get("name", ""), form.get("preset", ""), form.get("kind", PERSONAL)
        if kind not in KINDS:
            return self.render_new(csrf, name, preset, "Pick personal or shared.", status_code=400)
        if kind == PERSONAL and preset not in OFFERED:
            return self.render_new(csrf, name, error="Pick one of the layouts.", status_code=400, kind=kind)
        try:
            vault = self.store.create_vault(account.id, name, kind=kind)
        except VaultNameError as exc:
            return self.render_new(csrf, name, preset, str(exc), status_code=400, kind=kind)
        await anyio.to_thread.run_sync(provision_vault, self.sealed, vault, preset, self.vaults.dav_url)
        return RedirectResponse(self.vault_url(vault), status_code=303)

    async def make_key(self, request: Request) -> Response:
        found = await self.submitted(request)
        if isinstance(found, Response):
            return found
        account, csrf, vault, form = found
        name = " ".join(form.get("name", "").split())[:64]
        if not name:
            notice = error_box("Give the key a name.")
            return await anyio.to_thread.run_sync(lambda: self.render_vault(account, csrf, vault, notice, 400))
        scopes = [READ_SCOPE, WRITE_SCOPE] if form.get("access") == "write" else [READ_SCOPE]
        key = self.store.create_key(account.id, name, scopes, vault_id=vault.id)
        notice = (
            f'<div class="notice"><p>The key {esc(name)} reaches {esc(vault.name)} and is shown once. Copy it now.</p>'
            f"{copy_field('new-key', key)}</div>"
        )
        return await anyio.to_thread.run_sync(self.render_vault, account, csrf, vault, notice)

    async def revoke_key(self, request: Request) -> Response:
        found = await self.submitted(request)
        if isinstance(found, Response):
            return found
        account, _, vault, form = found
        self.store.revoke_key(account.id, form.get("key", ""), vault.id)
        return RedirectResponse(self.vault_url(vault), status_code=303)

    async def disconnect(self, request: Request) -> Response:
        found = await self.submitted(request)
        if isinstance(found, Response):
            return found
        account, _, vault, form = found
        self.store.disconnect(vault.id, form.get("client", ""), account.id)
        return RedirectResponse(self.vault_url(vault), status_code=303)

    async def export(self, request: Request) -> Response:
        slug = request.path_params["slug"]
        session = await self.session(request, f"/account/vaults/{slug}/export")
        if isinstance(session, Response):
            return session
        vault = self.store.own_vault(session[0].id, slug)
        if vault is None or vault.slug != slug or vault.shared:
            return self.not_found()
        spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)  # noqa: SIM115
        try:
            await anyio.to_thread.run_sync(export_zip, self.vaults.for_subject(vault.id).backend, spool)
        except BaseException:
            spool.close()
            raise
        spool.seek(0)

        def chunks() -> Iterator[bytes]:
            with spool:
                while chunk := spool.read(CHUNK_BYTES):
                    yield chunk

        headers = {
            "Content-Disposition": f'attachment; filename="{vault.slug}-{date.today().isoformat()}.zip"',
            "Cache-Control": "no-store",
        }
        return StreamingResponse(chunks(), media_type="application/zip", headers=headers)

    async def upload(self, request: Request) -> Response:
        found = await self.owned(request)
        if isinstance(found, Response):
            return found
        account, csrf, vault, _ = found
        if vault.shared:
            notice = error_box("A shared vault takes notes through its connections, so each change is kept.")
            return await anyio.to_thread.run_sync(lambda: self.render_vault(account, csrf, vault, notice, 409))
        upload = (await request.form()).get("archive")
        if not isinstance(upload, UploadFile):
            notice = error_box("Choose a zip file.")
            return await anyio.to_thread.run_sync(lambda: self.render_vault(account, csrf, vault, notice, 400))
        try:
            counts = await anyio.to_thread.run_sync(import_zip, self.vaults.for_subject(vault.id).backend, upload.file)
        except (zipfile.BadZipFile, BadArchive) as exc:
            notice = error_box(str(exc) if isinstance(exc, BadArchive) else "That file is not a zip archive.")
            return await anyio.to_thread.run_sync(lambda: self.render_vault(account, csrf, vault, notice, 400))
        except LimitReached as exc:
            notice, status = error_box(f"Nothing was imported. {exc}"), exc.status
            return await anyio.to_thread.run_sync(lambda: self.render_vault(account, csrf, vault, notice, status))
        notice = notice_box(f"Imported: {counts.summary()}.")
        return await anyio.to_thread.run_sync(self.render_vault, account, csrf, vault, notice)

    async def delete(self, request: Request) -> Response:
        found = await self.owned(request)
        if isinstance(found, Response):
            return found
        account, csrf, vault, form = found
        if not self.browser.recent(request):
            return self.deletion.sign_in_again(csrf, 403, self.vault_url(vault), "a vault")
        if " ".join(form.get("name", "").split()) != vault.name:
            notice = error_box(f"Type the vault's name, {vault.name}, to delete it.")
            return await anyio.to_thread.run_sync(lambda: self.render_vault(account, csrf, vault, notice, 400))
        await anyio.to_thread.run_sync(erase_vault, self.vaults, self.sealed, vault)
        whose = " for every member, with its history" if vault.shared else ""
        gone = f"{vault.name} is deleted{whose}, with its notes, keys and connections."
        notice = notice_box(gone)
        return await anyio.to_thread.run_sync(self.render_list, account, notice)

    def routes(self, prefix: str, limited: Callable) -> list[Route]:
        upload: ASGIApp = RequestBodyLimitMiddleware(request_response(self.upload), MAX_UPLOAD_BYTES)
        base = prefix + "/account/vaults/{slug}"
        return [
            Route(prefix + "/", endpoint=to_account, methods=["GET"]),
            Route(prefix + "/account", endpoint=self.list_page, methods=["GET"]),
            Route(prefix + "/account/page.js", endpoint=app_script, methods=["GET"]),
            Route(prefix + "/account/vaults/new", endpoint=self.new_page, methods=["GET"]),
            Route(prefix + "/account/vaults", endpoint=limited(self.create), methods=["POST"]),
            Route(base, endpoint=self.vault_page, methods=["GET"]),
            Route(base + "/layout", endpoint=limited(self.pick_layout), methods=["POST"]),
            Route(base + "/keys", endpoint=limited(self.make_key), methods=["POST"]),
            Route(base + "/keys/revoke", endpoint=limited(self.revoke_key), methods=["POST"]),
            Route(base + "/connections/disconnect", endpoint=limited(self.disconnect), methods=["POST"]),
            Route(base + "/export", endpoint=self.export, methods=["GET"]),
            Route(base + "/import", endpoint=upload, methods=["POST"]),
            Route(base + "/delete", endpoint=limited(self.delete), methods=["POST"]),
            *self.shared.routes(base, limited),
        ]


def waiting_notice(waiting: list[Waiting]) -> str:
    lines = "".join(
        f"<p>You are invited to {esc(item.vault.name)}, but you already reach {esc(item.clash.name)} at the address"
        f" <code>{esc(item.vault.slug)}</code>. Rename {esc(item.clash.name)}, then open this page again to join.</p>"
        for item in waiting
    )
    return f'<div class="notice">{lines}</div>' if lines else ""


async def to_account(request: Request) -> RedirectResponse:
    return RedirectResponse(request.url.path.rstrip("/") + "/account", status_code=303)


def broken_box(text: str | None) -> str:
    lines = broken_schema_lines(schema_from_text(text)) if text is not None else []
    if not lines:
        return ""
    said = [f"<p>{esc(line)}</p>" for line in lines if not line.startswith("- ")]
    items = "".join(f"<li>{esc(line.removeprefix('- '))}</li>" for line in lines if line.startswith("- "))
    return f'<div class="notice">{"".join(said)}{f"<ul>{items}</ul>" if items else ""}</div>'


def folder_rows(schema: Schema, typed: bool) -> list[str]:
    root = schema.data.get("root_description")
    rows = [f"<tr><td>the root</td><td>{esc(str(root))}</td>{'<td></td>' if typed else ''}</tr>"] if root else []
    for folder in schema.folders.values():
        hidden = " Not searched unless asked for." if not folder.searched else ""
        kinds = f"<td>{esc(', '.join(folder.kinds))}</td>" if typed else ""
        rows.append(
            f"<tr><td><code>{esc(folder.name)}/</code></td><td>{esc(folder.description)}{hidden}</td>{kinds}</tr>"
        )
    return rows


def layout_contents(text: str) -> str:
    schema = schema_from_text(text)
    typed = any(folder.kinds for folder in schema.folders.values())
    rows = folder_rows(schema, typed)
    folders = (
        table(["Folder", "What goes there", *(["Kinds"] if typed else [])], rows)
        if rows
        else '<p class="small muted">This layout names no folders; a note can go anywhere.</p>'
    )
    required = ", ".join(schema.required_fields) or "no field"
    facts = [("Every note needs", required)]
    if schema.area_required_in:
        facts.append(("Also an area in", ", ".join(f"{name}/" for name in schema.area_required_in)))
    if schema.kind_values:
        facts.append(("Kinds", ", ".join(schema.kind_values)))
    listed = "".join(f"<dt>{esc(label)}</dt><dd>{esc(value)}</dd>" for label, value in facts)
    return f"""{folders}<dl class="kv">{listed}</dl>
<details><summary class="btn secondary small">Show {esc(SCHEMA_KEY)}</summary>
<pre class="diff">{esc(text)}</pre></details>"""


def layout_options(selected: str) -> str:
    return "".join(
        f'<label class="option"><input type="radio" name="preset" value="{esc(name)}" required'
        f"{' checked' if name == selected else ''}><b>{esc(PRESETS[name].title)}</b>"
        f"<span>{esc(PRESETS[name].summary)}</span></label>"
        for name in OFFERED
    )
