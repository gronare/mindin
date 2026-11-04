from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from mindin import index as index_module
from mindin.index import Index
from mindin.server import build_server
from mindin.vault import Vault, open_vault

NOTE = "---\ntitle: Probe\ndate: 2026-08-01\nupdated: 2026-08-01\ntags: [orchard]\nstatus: active\n---\n\nrad\n"


def hold_write_lock(path: Path) -> sqlite3.Connection:
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO meta(k, v) VALUES('probe', '1')")
    return holder


def test_the_index_opens_at_once_while_another_process_writes(tmp_path: Path, vault: Vault):
    path = tmp_path / "shared.sqlite"
    Index(path, vault.backend).close()
    holder = hold_write_lock(path)
    started = time.time()
    opened = Index(path, vault.backend)
    assert time.time() - started < 1.0
    holder.execute("ROLLBACK")
    opened.close()


def test_a_write_waits_for_the_other_writer_instead_of_failing(tmp_path: Path, vault: Vault):
    path = tmp_path / "shared.sqlite"
    Index(path, vault.backend).close()
    holder = hold_write_lock(path)

    def release() -> None:
        time.sleep(1.5)
        holder.execute("COMMIT")

    threading.Thread(target=release, daemon=True).start()
    opened = Index(path, vault.backend)
    started = time.time()
    opened.upsert("Projects/probe.md", NOTE, "v1", 0.0, len(NOTE))
    assert time.time() - started >= 1.0
    assert opened.note("Projects/probe.md") is not None
    opened.close()


def test_a_write_gives_up_when_the_lock_outlives_the_busy_timeout(tmp_path: Path, vault: Vault, monkeypatch):
    monkeypatch.setattr(index_module, "BUSY_TIMEOUT_MS", 200)
    path = tmp_path / "stuck.sqlite"
    Index(path, vault.backend).close()
    holder = hold_write_lock(path)
    opened = Index(path, vault.backend)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        opened.upsert("Projects/probe.md", NOTE, "v1", 0.0, len(NOTE))
    holder.execute("ROLLBACK")
    opened.close()


def test_the_vault_opens_its_index_only_when_asked(vault_dir: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "cache"))
    opened = open_vault()
    assert not opened.index_opened
    assert not list((tmp_path / "cache").glob("*.sqlite"))
    build_server(opened)
    assert not opened.index_opened
    opened.index.sync(force=True)
    assert opened.index_opened
    opened.close()
    assert not opened.index_opened
