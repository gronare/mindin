from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mindin.auth.database import SqliteDatabase
from mindin.auth.hosted_store import HostedStore
from mindin.auth.identities import Identities
from mindin.extensions import Extension


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
