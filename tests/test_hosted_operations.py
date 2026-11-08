from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from hosted_support import Hosted, hosted

from mindin import provision
from mindin.backends import SCHEMA_KEY
from mindin.hosted.accounts.deletion import erase_account, erase_vault
from mindin.hosted.accounts.history import NothingToRestore, restore_change
from mindin.hosted.accounts.import_export import import_zip
from mindin.hosted.accounts.keys import SealedVaults, VaultKeys
from mindin.hosted.accounts.layout import BUILT_IN, LayoutKept, provision_vault, switch_layout
from mindin.hosted.accounts.vaults import SHARED, Vault
from mindin.hosted.orgs.mail import RESEND_AFTER, OrgMail
from mindin.hosted.orgs.organisations import Organisations
from mindin.hosted.signin.email import Mailer
from mindin.presets import OFFERED, SHARED_PRESET
from mindin.shared import Actor
from mindin.vault import SubjectVaults


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


def sealed_of(site: Hosted) -> SealedVaults:
    return SealedVaults(site.store, VaultKeys(site.store, site.keyring), "users")


def new_vault(site: Hosted, email: str, name: str, kind: str = "personal") -> Vault:
    account, _ = site.store.invite(email)
    return site.store.create_vault(account.id, name, kind=kind)


def preset_of(site: Hosted, vault: Vault) -> str | None:
    backend, _ = sealed_of(site).for_vault(vault.id)
    return provision.named_preset(provision.stored(backend, SCHEMA_KEY))


@pytest.mark.anyio
async def test_provisioning_writes_the_picked_layout_and_a_shared_vault_keeps_a_shared_one(site: Hosted):
    personal = new_vault(site, "ann@example.com", "Notes two")
    shared = new_vault(site, "ann@example.com", "Club", kind=SHARED)
    provision_vault(sealed_of(site), personal, OFFERED[-1], "http://localhost/dav/")
    provision_vault(sealed_of(site), shared, OFFERED[-1], "http://localhost/dav/")
    assert preset_of(site, personal) == OFFERED[-1]
    assert preset_of(site, shared) == SHARED_PRESET


@pytest.mark.anyio
async def test_switching_a_layout_says_what_happened_and_keeps_the_built_in_one(site: Hosted, vaults: SubjectVaults):
    vault = new_vault(site, "ann@example.com", "Notes two")
    first, second = OFFERED[0], OFFERED[-1]
    assert switch_layout(vaults, vault, first).startswith("This vault now uses")
    assert "No note was moved or deleted." not in switch_layout(vaults, vault, first)
    assert switch_layout(vaults, vault, first).startswith("This vault already uses")
    assert switch_layout(vaults, vault, second).endswith("No note was moved or deleted.")
    assert preset_of(site, vault) == second
    built_in = new_vault(site, "ann@example.com", "Built in")
    provision_vault(sealed_of(site), built_in, BUILT_IN, "http://localhost/dav/")
    with pytest.raises(LayoutKept):
        switch_layout(vaults, built_in, second)


@pytest.mark.anyio
async def test_importing_a_zip_writes_its_files_into_the_vault(site: Hosted, vaults: SubjectVaults):
    vault = new_vault(site, "ann@example.com", "Notes two")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as written:
        written.writestr("Areas/garden.md", "---\ntitle: Garden\n---\n\nPrune in March.\n")
    archive.seek(0)
    counts = import_zip(vaults.for_subject(vault.id).backend, archive)
    assert counts.written == 1
    data, _ = vaults.for_subject(vault.id).backend.get_bytes("Areas/garden.md")
    assert b"Prune in March." in data


@pytest.mark.anyio
async def test_erasing_a_vault_and_an_account_leaves_nothing_stored(site: Hosted, vaults: SubjectVaults, storage: Path):
    sealed = sealed_of(site)
    kept = new_vault(site, "ann@example.com", "Kept")
    gone = new_vault(site, "ann@example.com", "Gone")
    provision_vault(sealed, kept, OFFERED[0], "http://localhost/dav/")
    provision_vault(sealed, gone, OFFERED[0], "http://localhost/dav/")
    assert (storage / "users" / gone.id).is_dir()
    erase_vault(vaults, sealed, gone)
    assert site.store.vault(gone.id) is None and not (storage / "users" / gone.id).exists()
    assert (storage / "users" / kept.id).is_dir()
    owner = site.store.account("ann@example.com")
    erase_account(vaults, sealed, owner)
    assert site.store.account("ann@example.com") is None and site.store.vault(kept.id) is None
    assert not (storage / "users" / kept.id).exists()


@pytest.mark.anyio
async def test_an_invitation_mail_goes_once_until_the_resend_wait_passes(site: Hosted):
    organisations = Organisations(site.store, site.clock)
    acme = organisations.create("Acme", "ada@acme.test")
    site.store.invite("cy@elsewhere.test")
    organisations.add_member(acme, "cy@elsewhere.test")
    item = organisations.invitation(acme, "cy@elsewhere.test")
    silent = OrgMail(organisations, None, "http://localhost/", site.clock)
    assert not silent.sends and not silent.invite(item)
    sending = OrgMail(organisations, Mailer(site.config.smtp, site.smtp.factory), "http://localhost/", site.clock)
    assert sending.invite(item)
    assert site.smtp.messages[-1]["Subject"] == "Acme invites you to its vaults"
    mailed = organisations.invitation(acme, "cy@elsewhere.test")
    assert sending.waiting(mailed)
    site.clock.advance(RESEND_AFTER + 1)
    assert not sending.waiting(mailed)


@pytest.mark.anyio
async def test_restoring_an_unknown_change_is_refused_without_a_page(site: Hosted, vaults: SubjectVaults):
    club = new_vault(site, "ann@example.com", "Club", kind=SHARED)
    actor = Actor("ann@example.com", club.account_id, "test")
    with pytest.raises(NothingToRestore, match="no previous version"):
        restore_change(sealed_of(site).history(club.id), 999, actor, vaults.for_subject(club.id).backend)
