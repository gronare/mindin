from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from hosted_support import call_tool, csrf_of, hosted, text_of

from mindin.cli import main
from mindin.hosted.accounts.store import HostedStore
from mindin.hosted.signin.identities import Identities, Proof
from mindin.vault import SubjectVaults

PRUNING = (
    "---\ntitle: Pruning\ndate: 2026-09-01\nupdated: 2026-09-01\nstatus: draft\ntags: [orchard]\nkind: system\n"
    "---\n\nPrune the apple trees in March.\n"
)


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
    assert "ann@example.com  active  1 vaults  1 keys" in lines[0]
    assert re.search(r"  \d{4}-\d\d-\d\d \d\d:\d\d UTC$", lines[0])
    assert "never signed in" not in lines[0]
    assert "bo@example.com  invited  1 vaults  0 keys  never signed in" in lines[1]


def test_suspend_and_activate_by_email_or_id(store: HostedStore, capsys: pytest.CaptureFixture[str]):
    account, _ = store.invite("ann@example.com")
    assert main(["accounts", "suspend", "ann@example.com"]) == 0
    assert "ann@example.com is suspended" in capsys.readouterr().out
    assert store.account(account.id).status == "suspended"
    assert main(["accounts", "activate", account.id]) == 0
    assert store.account(account.id).status == "active"
    assert store.account(account.id).status_source == "operator"


def test_an_unknown_account_is_an_error(store: HostedStore, capsys: pytest.CaptureFixture[str]):
    assert main(["accounts", "suspend", "nobody@example.com"]) == 1
    assert "no account" in capsys.readouterr().err


def test_a_suspended_invite_is_not_activated_by_signing_in(store: HostedStore):
    store.invite("ann@example.com")
    store.set_status("ann@example.com", "suspended")
    account = Identities(store).sign_in(Proof("oidc", "sub-ann", ("ann@example.com",)), "invite").account
    assert account is not None and account.status == "suspended"


def test_status_can_be_set_by_another_source(store: HostedStore):
    store.invite("ann@example.com")
    account = store.set_status("ann@example.com", "suspended", source="script")
    assert account.status_source == "script"
    with pytest.raises(ValueError):
        store.set_status("ann@example.com", "deleted")  # type: ignore[arg-type]


def test_columns_and_tables_the_store_does_not_know_are_left_as_they_are(tmp_path: Path):
    path = tmp_path / "hosted.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE accounts (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE, sub TEXT UNIQUE,"
            " name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, status_source TEXT NOT NULL DEFAULT 'operator',"
            " status_changed_at INTEGER NOT NULL, created_at INTEGER NOT NULL, last_login_at INTEGER,"
            " extra_flag TEXT NOT NULL DEFAULT 'kept', extra_moment INTEGER);"
            "CREATE TABLE extra_things (id TEXT PRIMARY KEY);"
            "INSERT INTO extra_things VALUES ('thing');"
            "INSERT INTO accounts(id, email, status, status_changed_at, created_at, extra_flag, extra_moment)"
            " VALUES ('acc_old', 'old@example.com', 'active', 1, 1, 'set', 42);"
        )
    store = HostedStore(tmp_path)
    store.invite("new@example.com")
    assert store.account("old@example.com").status == "active"
    with store.connect() as connection:
        rows = connection.execute("SELECT email, extra_flag, extra_moment FROM accounts ORDER BY email").fetchall()
        things = connection.execute("SELECT id FROM extra_things").fetchall()
    assert [tuple(row) for row in rows] == [("new@example.com", "kept", None), ("old@example.com", "set", 42)]
    assert [tuple(row) for row in things] == [("thing",)]


def test_add_key_writes_the_value_only_to_a_new_private_file(
    store: HostedStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    account, _ = store.invite("ann@example.com")
    store.set_status("ann@example.com", "active")
    out = tmp_path / "ann.key"
    assert main(["accounts", "add-key", "ann@example.com", "agent", "--write", "--out", str(out)]) == 0
    printed = capsys.readouterr()
    value = out.read_text().strip()
    assert value.startswith("nvm_")
    assert out.stat().st_mode & 0o777 == 0o600
    assert value not in printed.out + printed.err
    [key] = store.keys(account.id)
    assert (key["name"], key["scopes"]) == ("agent", "vault:read vault:write")
    vault = store.default_vault(account.id)
    assert f"made the key {key['id']} 'agent' for ann@example.com's {vault.slug}, read and write" in printed.out
    grant = store.key_grant(value)
    assert grant is not None and grant.vault.id == vault.id


def test_add_key_reads_only_without_write_and_takes_a_vault(store: HostedStore, tmp_path: Path):
    account, _ = store.invite("ann@example.com")
    store.set_status("ann@example.com", "active")
    workshop = store.create_vault(account.id, "Workshop", "workshop")
    out = tmp_path / "ann.key"
    assert main(["accounts", "add-key", "ann@example.com", "reader", "--vault", "workshop", "--out", str(out)]) == 0
    grant = store.key_grant(out.read_text().strip())
    assert grant is not None and grant.vault.id == workshop.id and grant.scopes == ["vault:read"]
    assert main(["accounts", "add-key", "ann@example.com", "x", "--vault", "nope", "--out", str(tmp_path / "x")]) == 1
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize("status", ["invited", "suspended"])
def test_add_key_refuses_an_account_that_is_not_active(
    store: HostedStore, tmp_path: Path, status: str, capsys: pytest.CaptureFixture[str]
):
    account, _ = store.invite("ann@example.com")
    store.set_status("ann@example.com", status)  # type: ignore[arg-type]
    out = tmp_path / "ann.key"
    assert main(["accounts", "add-key", "ann@example.com", "agent", "--out", str(out)]) == 1
    assert f"ann@example.com is {status}" in capsys.readouterr().err
    assert not out.exists()
    assert store.keys(account.id) == []


def test_add_key_refuses_a_file_that_exists(store: HostedStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    account, _ = store.invite("ann@example.com")
    store.set_status("ann@example.com", "active")
    out = tmp_path / "ann.key"
    out.write_text("kept\n")
    assert main(["accounts", "add-key", "ann@example.com", "agent", "--out", str(out)]) == 1
    assert "exists" in capsys.readouterr().err
    assert out.read_text() == "kept\n"
    assert store.keys(account.id) == []


def test_remove_key_revokes_a_key_by_name_or_id(store: HostedStore, capsys: pytest.CaptureFixture[str]):
    account, _ = store.invite("ann@example.com")
    store.set_status("ann@example.com", "active")
    agent = store.create_key(account.id, "agent", ["vault:read"])
    store.create_key(account.id, "twin", ["vault:read"])
    twin = store.create_key(account.id, "twin", ["vault:read"])
    assert main(["accounts", "remove-key", "ann@example.com", "agent"]) == 0
    assert "revoked the key" in capsys.readouterr().out
    assert store.key_grant(agent) is None
    assert main(["accounts", "remove-key", "ann@example.com", "twin"]) == 2
    assert "several keys named 'twin'" in capsys.readouterr().err
    assert main(["accounts", "remove-key", "ann@example.com", twin[4:16]]) == 0
    assert store.key_grant(twin) is None
    assert main(["accounts", "remove-key", "ann@example.com", "agent"]) == 1
    assert "has no key 'agent'" in capsys.readouterr().err


@pytest.mark.anyio
async def test_an_operator_key_for_an_account_that_never_signed_in_reaches_mcp(
    vaults: SubjectVaults, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async with hosted(vaults, tmp_path) as site:
        monkeypatch.setenv("VAULT_AUTH_DIR", str(site.config.auth_dir))
        out = tmp_path / "agent.key"
        assert main(["accounts", "invite", "ann@example.com"]) == 0
        assert main(["accounts", "activate", "ann@example.com"]) == 0
        assert main(["accounts", "add-key", "ann@example.com", "agent", "--write", "--out", str(out)]) == 0
        assert site.store.account("ann@example.com").last_login_at is None
        key = out.read_text().strip()
        async with site.browser() as http:
            written = await call_tool(http, key, "write_file", {"path": "Areas/pruning.md", "content": PRUNING})
            read = await call_tool(http, key, "read_file", {"path": "Areas/pruning.md"})
            limits = await http.get("/api/vault/limits", headers={"Authorization": f"Bearer {key}"})
    assert limits.status_code == 200 and limits.json()["write"] is True
    assert not written.get("isError"), text_of(written)
    assert "Prune the apple trees in March." in text_of(read)


@pytest.mark.anyio
async def test_the_owner_sees_and_removes_a_key_the_operator_made(
    vaults: SubjectVaults, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async with hosted(vaults, tmp_path) as site:
        monkeypatch.setenv("VAULT_AUTH_DIR", str(site.config.auth_dir))
        http = await site.member("ann@example.com")
        out = tmp_path / "agent.key"
        assert main(["accounts", "add-key", "ann@example.com", "agent", "--out", str(out)]) == 0
        own = site.key_for("ann@example.com", name="laptop")
        async with http:
            page = await http.get("/account/vaults/notes")
            rows = {row.split("</td>")[0] for row in page.text.split("<tr><td>")[1:]}
            assert 'agent <span class="small muted">made by the operator</span>' in rows
            assert "laptop" in rows
            key_id = out.read_text().strip()[4:16]
            revoked = await http.post(
                "/account/vaults/notes/keys/revoke", data={"csrf": csrf_of(page.text), "key": key_id}
            )
            assert revoked.status_code == 303
            after = await http.get("/account/vaults/notes")
    assert "made by the operator" not in after.text and "laptop" in after.text
    assert site.store.key_grant(out.read_text().strip()) is None
    assert site.store.key_grant(own) is not None
