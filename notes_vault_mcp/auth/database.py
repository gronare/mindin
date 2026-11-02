from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Protocol


class Connection(Protocol):
    """One open connection: qmark parameters, rows readable by column name and by index."""

    def execute(self, sql: str, parameters: Sequence[Any] = (), /) -> Any: ...


Step = Callable[[Any], None]


class Database(Protocol):
    """Where the account store keeps its tables."""

    dialect: str
    IntegrityError: type[Exception]

    def connect(self) -> AbstractContextManager[Connection]:
        """A connection that commits when the block ends cleanly and rolls back when it raises."""
        ...

    def tables(self, connection: Connection) -> list[str]: ...

    def columns(self, connection: Connection, table: str) -> list[str]: ...

    def create(self, connection: Connection, script: str) -> None:
        """Run a script of CREATE … IF NOT EXISTS statements, every time the store opens."""
        ...

    def migrate(self, steps: Sequence[Step]) -> None:
        """Run, in one transaction, the upgrade steps numbered from 1 that this database has not run yet."""
        ...


class SqliteDatabase:
    dialect = "sqlite"
    IntegrityError: type[Exception] = sqlite3.IntegrityError

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def tables(self, connection: sqlite3.Connection) -> list[str]:
        return tables(connection)

    def columns(self, connection: sqlite3.Connection, table: str) -> list[str]:
        return columns(connection, table)

    def create(self, connection: sqlite3.Connection, script: str) -> None:
        connection.executescript(script)

    def migrate(self, steps: Sequence[Step]) -> None:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                for number, step in enumerate(steps, start=1):
                    if version < number:
                        step(connection)
                if version < len(steps):
                    connection.execute(f"PRAGMA user_version = {len(steps)}")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()


def tables(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]


def columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def has_table(connection: sqlite3.Connection, table: str) -> bool:
    return table in tables(connection)


def add_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    if has_table(connection, table) and column not in columns(connection, table):
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN {column} {declaration}')


def account_tables(database: Database, connection: Connection) -> list[str]:
    return [table for table in database.tables(connection) if "account_id" in database.columns(connection, table)]
