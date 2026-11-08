from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mindin.extensions import Extension
from mindin.hosted.accounts.store import HostedStore
from mindin.hosted.database import SqliteDatabase
from mindin.hosted.signin.identities import Identities, Proof


class Clash(Exception):
    pass


class OwnErrors(SqliteDatabase):
    IntegrityError = Clash

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        try:
            with super().connect() as connection:
                yield connection
        except sqlite3.IntegrityError as error:
            raise Clash(str(error)) from error


class PortableOnly:
    def __init__(self, raw: sqlite3.Connection) -> None:
        self.raw = raw

    def execute(self, sql: str, parameters: Sequence[Any] = (), /) -> sqlite3.Cursor:
        if sql.startswith(("BEGIN", "PRAGMA")):
            raise sqlite3.OperationalError(f"not portable SQL: {sql}")
        return self.raw.execute(sql, parameters)


class Portable(SqliteDatabase):
    @contextmanager
    def connect(self) -> Iterator[Any]:
        with super().connect() as connection:
            yield PortableOnly(connection)


class Elsewhere(Extension):
    def __init__(self, path: Path) -> None:
        self.path = path

    def database(self, directory: Path) -> SqliteDatabase:
        return SqliteDatabase(self.path)


def emails(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        return [row[0] for row in connection.execute("SELECT email FROM accounts")]


def test_the_store_keeps_its_tables_in_hosted_sqlite_by_default(tmp_path: Path):
    store = HostedStore(tmp_path, extensions=())
    store.invite("ann@example.com")
    assert isinstance(store.database, SqliteDatabase)
    assert emails(tmp_path / "hosted.sqlite") == ["ann@example.com"]


def test_a_database_handed_to_the_store_holds_its_tables(tmp_path: Path):
    store = HostedStore(tmp_path / "auth", extensions=(), database=SqliteDatabase(tmp_path / "elsewhere.sqlite"))
    store.invite("ann@example.com")
    assert emails(tmp_path / "elsewhere.sqlite") == ["ann@example.com"]
    assert not (tmp_path / "auth" / "hosted.sqlite").exists()


def test_an_extension_can_supply_the_database(tmp_path: Path):
    store = HostedStore(tmp_path / "auth", extensions=(Elsewhere(tmp_path / "shared.sqlite"),))
    store.invite("ann@example.com")
    assert emails(tmp_path / "shared.sqlite") == ["ann@example.com"]
    assert not (tmp_path / "auth" / "hosted.sqlite").exists()


def test_a_duplicate_is_recognised_by_the_databases_own_error(tmp_path: Path):
    store = HostedStore(tmp_path, extensions=(), database=OwnErrors(tmp_path / "own.sqlite"))
    account, _ = store.invite("ann@example.com")
    identities = Identities(store)
    assert identities.add_passkey(account.id, "credential-1", b"key", 0, "Laptop")
    assert not identities.add_passkey(account.id, "credential-1", b"key", 0, "Laptop")


def two_ways_in(store: HostedStore) -> str:
    identities = Identities(store)
    store.invite("ann@example.com")
    account = identities.sign_in(Proof("oidc", "sub-ann", ("ann@example.com",)), "invite").account
    assert account is not None
    assert identities.link(account.id, Proof("github", "5", ("ann@example.com",)))
    return account.id


def test_removing_a_way_in_needs_no_sqlite_only_statement(tmp_path: Path):
    store = HostedStore(tmp_path, extensions=())
    account_id = two_ways_in(store)
    identities = Identities(store)
    store.database = Portable(tmp_path / "hosted.sqlite")
    assert identities.remove(account_id, "github", "nobody") == "missing"
    assert identities.remove(account_id, "github", "5") == "removed"
    assert identities.remove(account_id, "oidc", "sub-ann") == "last"
    assert [way.method for way in identities.ways_in(account_id)] == ["oidc"]


def race(store: HostedStore, account_id: str, ways: Sequence[tuple[str, str]]) -> list[str]:
    start = threading.Barrier(len(ways))
    results: list[str] = []

    def remove(method: str, subject: str) -> None:
        identities = Identities(store)
        start.wait()
        results.append(identities.remove(account_id, method, subject))

    threads = [threading.Thread(target=remove, args=way) for way in ways]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_two_removals_racing_for_the_last_two_ways_in_leave_one(tmp_path: Path):
    for attempt in range(10):
        store = HostedStore(tmp_path / str(attempt), extensions=())
        account_id = two_ways_in(store)
        assert sorted(race(store, account_id, [("oidc", "sub-ann"), ("github", "5")])) == ["last", "removed"]
        assert len(Identities(store).ways_in(account_id)) == 1
