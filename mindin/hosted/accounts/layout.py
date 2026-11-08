from __future__ import annotations

from typing import TYPE_CHECKING

from mindin import provision
from mindin.backends import SCHEMA_KEY
from mindin.presets import CUSTOM_PRESET, PRESETS, SHARED_LAYOUTS, SHARED_PRESET

if TYPE_CHECKING:
    from mindin.hosted.accounts.keys import SealedVaults
    from mindin.hosted.accounts.vaults import Vault
    from mindin.vault import SubjectVaults

BUILT_IN = "default"
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


def titled(named: str, source: str | None = None) -> str:
    return f"{PRESETS[named].title}, made from {PRESETS[source].title}" if source else PRESETS[named].title


def describe(named: str, source: str | None = None) -> str:
    if named in PRESETS:
        return f"This vault uses {titled(named, source)}."
    return f"This vault's schema names {named!r}, a layout this version does not ship; it is read as written."


def layout_title(schema_text: str | None) -> str:
    named = provision.named_preset(schema_text)
    if named is None:
        return "no layout yet"
    if named in PRESETS:
        return f"{PRESETS[named].title.lower() if named == 'default' else PRESETS[named].title} layout"
    return f"{named} layout"


class LayoutKept(Exception):
    pass


def provision_vault(sealed: SealedVaults, vault: Vault, preset: str, dav_url: str) -> None:
    backend, _ = sealed.for_vault(vault.id)
    if vault.shared:
        provision.initialize(backend, preset=preset if preset in SHARED_LAYOUTS else SHARED_PRESET)
        return
    provision.initialize(backend, preset=preset, welcome={"DAV_URL": dav_url})


def switch_layout(vaults: SubjectVaults, vault: Vault, preset: str, start_from: str | None = None) -> str:
    backend = vaults.backend_factory(vault.id)[0]
    current = provision.named_preset(provision.stored(backend, SCHEMA_KEY))
    if current == BUILT_IN:
        raise LayoutKept("The built-in layout is kept; it is switched from the CLI only.")
    if current == preset and preset != CUSTOM_PRESET:
        return f"This vault already uses {PRESETS[preset].title}."
    source = start_from if preset == CUSTOM_PRESET else None
    welcome = {"DAV_URL": vaults.dav_url} if current is None else None
    result = provision.initialize(backend, force=current is not None, preset=preset, welcome=welcome, start_from=source)
    vaults.refresh(vault.id)
    vaults.publish(vault.id, "schema")
    moved = " No note was moved or deleted." if current is not None else ""
    return f"This vault now uses {titled(result.preset, source)}.{moved}"
