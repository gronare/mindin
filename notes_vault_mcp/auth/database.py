from __future__ import annotations

import sqlite3


def tables(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]


def columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def has_table(connection: sqlite3.Connection, table: str) -> bool:
    return table in tables(connection)


def add_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    if has_table(connection, table) and column not in columns(connection, table):
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN {column} {declaration}')


def account_tables(connection: sqlite3.Connection) -> list[str]:
    return [table for table in tables(connection) if "account_id" in columns(connection, table)]
