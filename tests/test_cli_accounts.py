from __future__ import annotations

from pathlib import Path

import pytest

from notes_vault_mcp.auth.hosted_store import HostedStore
from notes_vault_mcp.auth.identities import Identities, Proof
from notes_vault_mcp.cli import main


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HostedStore:
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path))
    return HostedStore(tmp_path)


def test_invite_adds_an_invited_account_once(store: HostedStore, capsys: pytest.CaptureFixture[str]):
    assert main(["accounts", "invite", "Ann@Example.com"]) == 0
    assert "ann@example.com invited" in capsys.readouterr().out
    assert main(["accounts", "invite", "ann@example.com"]) == 0
    assert "already has an account" in capsys.readouterr().out
    accounts = store.accounts()
    assert [(account.email, account.status) for account in accounts] == [("ann@example.com", "invited")]


def test_invite_refuses_something_that_is_not_an_address(store: HostedStore, capsys: pytest.CaptureFixture[str]):
    assert main(["accounts", "invite", "ann"]) == 2
    assert "not an email address" in capsys.readouterr().err


def test_list_shows_status_keys_and_last_sign_in(store: HostedStore, capsys: pytest.CaptureFixture[str]):
    assert main(["accounts", "list"]) == 0
    assert "no accounts yet" in capsys.readouterr().out
    store.invite("ann@example.com")
    account = Identities(store).sign_in(Proof("oidc", "sub-ann", ("ann@example.com",), name="Ann"), "invite").account
    store.create_key(account.id, "laptop", ["vault:read"])
    store.invite("bo@example.com")
    assert main(["accounts", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "ann@example.com  active  1 keys" in lines[0]
    assert "never signed in" not in lines[0]
    assert "bo@example.com  invited  0 keys  never signed in" in lines[1]


def test_suspend_and_activate_by_email_or_id(store: HostedStore, capsys: pytest.CaptureFixture[str]):
    account, _ = store.invite("ann@example.com")
    assert main(["accounts", "suspend", "ann@example.com"]) == 0
    assert "ann@example.com is suspended" in capsys.readouterr().out
    assert store.account(account.id).status == "suspended"
    assert main(["accounts", "activate", account.id]) == 0
    assert store.account(account.id).status == "active"
    assert store.account(account.id).status_source == "admin"


def test_an_unknown_account_is_an_error(store: HostedStore, capsys: pytest.CaptureFixture[str]):
    assert main(["accounts", "suspend", "nobody@example.com"]) == 1
    assert "no account" in capsys.readouterr().err


def test_a_suspended_invite_is_not_activated_by_signing_in(store: HostedStore):
    store.invite("ann@example.com")
    store.set_status("ann@example.com", "suspended")
    account = Identities(store).sign_in(Proof("oidc", "sub-ann", ("ann@example.com",)), "invite").account
    assert account is not None and account.status == "suspended"


def test_status_can_be_set_by_another_source_for_later_billing(store: HostedStore):
    store.invite("ann@example.com")
    account = store.set_status("ann@example.com", "suspended", source="billing")
    assert account.status_source == "billing"
    with pytest.raises(ValueError):
        store.set_status("ann@example.com", "deleted")  # type: ignore[arg-type]
