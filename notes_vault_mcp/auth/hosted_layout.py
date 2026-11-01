from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from notes_vault_mcp import provision
from notes_vault_mcp.auth.hosted_store import Account
from notes_vault_mcp.auth.hosted_transfer import subject_of
from notes_vault_mcp.auth.hosted_web import Browser, error_box, esc
from notes_vault_mcp.backends import SCHEMA_KEY, VaultBackend
from notes_vault_mcp.presets import OFFERED, PRESETS
from notes_vault_mcp.schema import broken_schema_lines, schema_from_text

if TYPE_CHECKING:
    from notes_vault_mcp.vault import SubjectVaults

SWITCHING = (
    "Switching writes a new schema and new Bases views. No note is moved, renamed or deleted: notes in folders "
    "the new layout does not name stay where they are and stay searchable. Each note is checked against the new "
    "rules the next time it is written, and lint lists the ones that no longer fit. The schema it replaces is "
    f"saved as {provision.PREVIOUS_SCHEMA_KEY}; views from the old layout that you never changed are removed."
)
EDITING = (
    f"The layout lives in {SCHEMA_KEY}, a file in your vault. To make it your own, edit it over WebDAV with any "
    "WebDAV client (Obsidian hides dot-folders, so it does not show it). If an edit does not hold, the vault keeps "
    "running on the layout the file names, and this page, context and lint say what is wrong."
)


def broken_box(text: str | None) -> str:
    lines = broken_schema_lines(schema_from_text(text)) if text is not None else []
    if not lines:
        return ""
    said = [f"<p>{esc(line)}</p>" for line in lines if not line.startswith("- ")]
    items = "".join(f"<li>{esc(line.removeprefix('- '))}</li>" for line in lines if line.startswith("- "))
    return f'<div class="warning">{"".join(said)}{f"<ul>{items}</ul>" if items else ""}</div>'


class Layouts:
    def __init__(self, browser: Browser, vaults: SubjectVaults) -> None:
        self.browser = browser
        self.vaults = vaults

    def backend(self, account: Account) -> VaultBackend:
        return self.vaults.backend_factory(subject_of(account))[0]

    def choices(self, current: str | None) -> str:
        return "".join(
            f'<label><input type="radio" name="preset" value="{esc(name)}" required'
            f"{' checked' if name == current else ''}> {esc(PRESETS[name].title)}: {esc(PRESETS[name].summary)}</label>"
            for name in OFFERED
        )

    def describe(self, named: str) -> str:
        if named in PRESETS:
            return f"Your vault uses {PRESETS[named].title}."
        return f"Your vault's schema names {named!r}, a layout this version does not ship; it is read as written."

    def section(self, account: Account, csrf: str) -> str:
        text = provision.stored(self.backend(account), SCHEMA_KEY)
        current = provision.named_preset(text)
        if current is None:
            heading = (
                "<h2>Pick a layout</h2><p>Your vault has no layout yet. Pick one to start; you can switch later.</p>"
            )
            button = "Use this layout"
        else:
            heading = f"<h2>Layout</h2><p>{esc(self.describe(current))}</p><p>{esc(SWITCHING)}</p>"
            button = "Switch layout"
        return f"""{heading}
{broken_box(text)}<p>{esc(EDITING)}</p>
<form method="post" action="{esc(self.browser.local("/account/layout"))}">
<input type="hidden" name="csrf" value="{esc(csrf)}">
<fieldset><legend>Layout</legend>
{self.choices(current)}
</fieldset>
<button type="submit">{button}</button>
</form>"""

    async def submit(self, request: Request) -> Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        account, csrf, form = submitted
        name = form.get("preset", "")
        if name not in OFFERED:
            return self.browser.render_account(account, csrf, error_box("Pick one of the layouts."), status_code=400)
        backend = self.backend(account)
        current = provision.named_preset(provision.stored(backend, SCHEMA_KEY))
        if current == name:
            notice = f"<p>Your vault already uses {esc(PRESETS[name].title)}.</p>"
            return self.browser.render_account(account, csrf, notice)
        welcome = {"DAV_URL": self.vaults.dav_url} if current is None else None
        result = provision.initialize(backend, force=current is not None, preset=name, welcome=welcome)
        self.vaults.refresh(subject_of(account))
        moved = " No note was moved or deleted." if current is not None else ""
        notice = f"<p>Your vault now uses {esc(PRESETS[result.preset].title)}.{moved}</p>"
        return self.browser.render_account(account, csrf, notice)

    def routes(self, prefix: str, limited: Callable) -> list[Route]:
        return [Route(prefix + "/account/layout", endpoint=limited(self.submit), methods=["POST"])]
