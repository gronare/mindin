from __future__ import annotations

import base64
from pathlib import Path

import pytest
from hosted_support import Hosted, call_tool, csrf_of, hosted, text_of

from mindin import notes, provision
from mindin.backends.local import LocalBackend
from mindin.guard import GuardSettings, findings
from mindin.hosted.orgs.organisations import Organisations
from mindin.presets import preset_data
from mindin.schema import load_schema, schema_problems
from mindin.vault import SubjectVaults, Vault

ON = GuardSettings(secrets=True, personal_data=("card_number", "personnummer"))
AWS_KEY = "AKIA" + "ZQ3XJ7QZPL3MNB6V"
PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----\n" + "MIIEow" + "Kq9" * 30 + "\n-----END RSA PRIVATE KEY-----"
CARD = "4111 1111 1111 1111"
PERSONNUMMER = "19121212-1212"
FRONT = (
    "---\ntitle: Settling payouts\ndate: 2026-11-01\nupdated: 2026-11-01\nstatus: active\nteam: payments\n"
    'author: Ada Lovelace\ntags: [payments]\nkind: howto\narea: "[[payments]]"\nrepos: [shop]\n---\n\n'
)
GUIDE = (
    FRONT + "Payouts run nightly from the shop's payout job. The provider key lives in the secret manager under "
    "payouts/provider, and the job reads it as PAYOUT_API_KEY. Order 2026-11-08 1234 shipped on 2026-11-09; call "
    "+46 70 123 45 67 or see commit 4f9c2ab1d3e5f60718293a4b5c6d7e8f90a1b2c3. Test with the card number 4111 "
    "described in the provider's docs, and password: <your password>.\n"
)


@pytest.mark.parametrize(
    ("text", "label"),
    [
        (f"aws_access_key_id = {AWS_KEY}", "an AWS access key"),
        (PRIVATE_KEY, "a private key"),
        (f"Charged {CARD} yesterday.", "a card number"),
        (f"Booked for {PERSONNUMMER}.", "a Swedish personal identity number"),
        ("Token ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8", "a GitHub token"),
        ("export DEPLOY_TOKEN=Zx81kQp0vLm2Rt7sYw4N", "a key or token"),
        ("password: hunter2!", "a password"),
        ("postgres://app:s3cr3t@db.internal/shop", "a password in an address"),
    ],
)
def test_the_guard_names_what_it_finds(text: str, label: str):
    assert label in [finding.label for finding in findings(text, ON)]


def test_the_guard_lets_a_normal_guide_through():
    assert findings(GUIDE, ON) == []


def test_the_guard_skips_lookalikes():
    assert findings("4111 1111 1111 1112 and 19121212-1213 and AKIAIOSFODNN7EXAMPLE", ON) == []


def test_the_wiki_turns_the_guard_on_and_names_unknown_detectors():
    assert preset_data("wiki")["bundle"]["guard"] == {"secrets": True, "personal_data": ["card_number", "personnummer"]}
    data = preset_data("wiki")
    data["bundle"]["guard"] = {"secrets": "yes", "personal_data": ["iban"]}
    assert schema_problems(data) == [
        "bundle.guard.secrets must be true or false",
        "bundle.guard.personal_data names 'iban', which is not one of: card_number, personnummer",
    ]


def test_a_personal_vault_with_the_guard_on_refuses_and_writes_nothing(tmp_path: Path):
    backend = LocalBackend(tmp_path / "vault")
    provision.initialize(backend, preset="wiki")
    vault = Vault(backend, load_schema(backend), index_path=tmp_path / "index.sqlite")
    with pytest.raises(notes.ValidationError, match=r"line 16: an AWS access key \(AKIA…\)\n(?!.*a key or token)"):
        notes.write(vault, "Guides/settling-payouts.md", GUIDE + f"\nKey: {AWS_KEY}\n")
    assert not (tmp_path / "vault" / "Guides" / "settling-payouts.md").exists()
    assert notes.write(vault, "Guides/settling-payouts.md", GUIDE).startswith("Written:")
    with pytest.raises(notes.ValidationError, match="a card number"):
        notes.append(vault, "Guides/settling-payouts.md", f"Charge back {CARD}.")


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


@pytest.mark.anyio
async def test_the_wiki_refuses_secrets_and_personal_data_through_claude_and_obsidian(site: Hosted):
    organisation = Organisations(site.store, site.clock).create("Acme", "ada@acme.test")
    async with site.browser() as ada:
        await site.sign_in(ada, "ada@acme.test")
        settings = await ada.get(f"/account/organisation/{organisation.id}")
        await ada.post(
            f"/account/organisation/{organisation.id}/vaults",
            data={"csrf": csrf_of(settings.text), "name": "Wiki", "slug": "wiki"},
        )
        account = site.store.account("ada@acme.test")
        vault = site.store.vault_at(account.id, "wiki")
        secret = site.store.create_key(account.id, "laptop", ["vault:read", "vault:write"], vault_id=vault.id)
        leaky = GUIDE + f"\nKey {AWS_KEY}\n\n{PRIVATE_KEY}\n\nCharged {CARD} by {PERSONNUMMER}.\n"
        refused = await call_tool(
            ada, secret, "write_file", {"path": "Guides/settling-payouts.md", "content": leaky}, path="/mcp?vault=wiki"
        )
        basic = {"Authorization": "Basic " + base64.b64encode(f"ada:{secret}".encode()).decode()}
        dav = await ada.put("/dav/vault/Guides/settling-payouts.md", content=leaky.encode(), headers=basic)
        listed = await call_tool(ada, secret, "list_files", {"prefix": "Guides/"}, path="/mcp?vault=wiki")
        fine = await call_tool(
            ada, secret, "write_file", {"path": "Guides/settling-payouts.md", "content": GUIDE}, path="/mcp?vault=wiki"
        )
    told = text_of(refused)
    assert refused.get("isError") and "Guides/settling-payouts.md was not written" in told
    for label in ("an AWS access key (AKIA…)", "a private key", "a card number", "a Swedish personal identity number"):
        assert label in told, told
    assert AWS_KEY not in told and CARD not in told and PERSONNUMMER not in told
    assert dav.status_code == 403 and "an AWS access key" in dav.text
    assert "settling-payouts" not in text_of(listed)
    assert not fine.get("isError"), text_of(fine)
