from __future__ import annotations

import sqlite3
from pathlib import Path

from notes_vault_mcp.index import Index, link_targets, path_list, shas_in
from notes_vault_mcp.search import search
from notes_vault_mcp.vault import Vault

OLD_FTS_SQL = """
CREATE TABLE notes (
  key TEXT PRIMARY KEY, version TEXT, mtime REAL, folder TEXT, stem TEXT,
  title TEXT, summary TEXT, status TEXT, kind TEXT, area TEXT,
  tags TEXT, paths TEXT, date TEXT, updated TEXT, superseded_by TEXT,
  valid INT, error TEXT, size INT, priority TEXT, source TEXT
);
CREATE VIRTUAL TABLE notes_fts USING fts5(
  title, summary, tags, body, key UNINDEXED,
  tokenize="unicode61 remove_diacritics 2"
);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
INSERT INTO notes(key, version, stem, title) VALUES('Areas/orchard.md', 'stale', 'orchard', 'orchard');
INSERT INTO notes_fts(title, summary, tags, body, key) VALUES('orchard', '', '', '', 'Areas/orchard.md');
INSERT INTO meta(k, v) VALUES('last_sync', '9999999999');
"""

UNSTEMMED_FTS_SQL = OLD_FTS_SQL.replace("body, key UNINDEXED", "body, stem, key UNINDEXED").replace(
    "INSERT INTO meta(k, v) VALUES('last_sync', '9999999999');",
    "INSERT INTO meta(k, v) VALUES('last_sync', '9999999999');\nINSERT INTO meta(k, v) VALUES('languages', 'sv,en');",
)


class CountingBackend:
    def __init__(self, inner):
        self.inner = inner
        self.fetched: list[str] = []

    def list(self):
        return self.inner.list()

    def get(self, key):
        self.fetched.append(key)
        return self.inner.get(key)

    def put(self, key, text, expected_version=None):
        return self.inner.put(key, text, expected_version)

    def delete(self, key):
        self.inner.delete(key)

    def move(self, src, dst):
        self.inner.move(src, dst)


def test_sync_indexes_every_markdown_note(vault: Vault):
    keys = {note.key for note in vault.index.all_notes()}
    assert "Areas/orchard.md" in keys
    assert "Archive/old-plan.md" in keys
    assert not any(key.endswith(".base") or key.startswith(".obsidian") for key in keys)


def test_sync_records_frontmatter_fields(vault: Vault):
    note = vault.index.note("Projects/orchard-fresh.md")
    assert note.title == "Orchard: bokningswidget i checkout"
    assert note.status == "active"
    assert note.area == "[[orchard]]"
    assert note.tags == ["orchard", "booking", "ui"]
    assert note.folder == "Projects"
    assert note.stem == "orchard-fresh"


def test_sync_marks_broken_frontmatter_invalid(vault: Vault):
    note = vault.index.note("Resources/broken-yaml.md")
    assert note.valid is False
    assert note.error


def test_sync_is_throttled_until_forced(vault: Vault):
    assert vault.index.sync() == 0


def test_sync_refetches_only_the_changed_note(vault: Vault, vault_dir: Path):
    counting = CountingBackend(vault.index.backend)
    vault.index.backend = counting
    changed = vault_dir / "Areas" / "workshop.md"
    changed.write_text(changed.read_text(encoding="utf-8") + "\nEn ny rad.\n", encoding="utf-8")
    assert vault.index.sync(force=True) == 1
    assert counting.fetched == ["Areas/workshop.md"]


def test_sync_removes_a_deleted_note(vault: Vault, vault_dir: Path):
    (vault_dir / "Areas" / "workshop.md").unlink()
    vault.index.sync(force=True)
    assert vault.index.note("Areas/workshop.md") is None


def test_sync_stores_paths_both_as_written_and_expanded(vault: Vault):
    paths = vault.index.note("Areas/workshop.md").paths
    assert "~/workshop" in paths
    assert str(Path("~/workshop").expanduser()) in paths


def test_rebuild_reindexes_everything(vault: Vault):
    before = len(vault.index.all_notes())
    assert vault.index.rebuild() == before
    assert len(vault.index.all_notes()) == before


def test_shas_need_a_letter_and_a_digit():
    assert shas_in("9ca31382 deadbeef 1234567 abcdefa f00d1234") == ["9ca31382", "f00d1234"]


def test_link_targets_drop_alias_heading_and_folder():
    assert link_targets("[[orchard]] [[Areas/Workshop|hem]] [[note#rubrik]]") == ["note", "orchard", "workshop"]


def test_path_list_splits_on_comma_space():
    assert path_list("~/a, /b")[:1] == ["~/a"]
    assert "/b" in path_list("~/a, /b")


def test_an_index_without_the_stem_column_is_rebuilt_and_searchable_by_stem(vault: Vault, tmp_path: Path):
    path = tmp_path / "old" / "index.sqlite"
    path.parent.mkdir()
    old = sqlite3.connect(path)
    old.executescript(OLD_FTS_SQL)
    old.close()
    index = Index(path, vault.index.backend)
    try:
        assert "stem" in {row["name"] for row in index.db.execute("PRAGMA table_info(notes_fts)")}
        assert index.all_notes() == []
        index.sync()
        found = search(index, vault.schema, "orchard-fresh")
        assert [row.note.key for row in found.rows][0] == "Projects/orchard-fresh.md"
        assert index.db.execute("SELECT key FROM notes_fts WHERE notes_fts MATCH 'stem:\"orchard fresh\"'").fetchone()
    finally:
        index.close()


def fts_columns(index: Index) -> set[str]:
    return {row["name"] for row in index.db.execute("PRAGMA table_info(notes_fts)")}


def test_an_index_without_the_stemmed_column_is_rebuilt_and_finds_inflections(vault: Vault, tmp_path: Path):
    path = tmp_path / "old" / "index.sqlite"
    path.parent.mkdir()
    old = sqlite3.connect(path)
    old.executescript(UNSTEMMED_FTS_SQL)
    old.close()
    index = Index(path, vault.index.backend, vault.schema.languages)
    try:
        assert "stemmed" in fts_columns(index)
        assert index.all_notes() == []
        index.sync()
        assert "Areas/orchard.md" in [row.note.key for row in search(index, vault.schema, "lundkoden").rows]
    finally:
        index.close()


def test_the_index_records_the_languages_it_was_stemmed_in(vault: Vault):
    assert vault.index.meta("languages") == "sv,en"
    assert vault.index.algorithms == ("swedish", "english")


def test_changed_languages_rebuild_the_index(vault: Vault):
    path = vault.index.path
    vault.close()
    index = Index(path, vault.backend, ("sv",))
    try:
        assert index.meta("languages") == "sv"
        for table in ("notes", "notes_fts", "shas", "links"):
            assert index.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
        index.sync()
        stemmed = index.db.execute("SELECT stemmed FROM notes_fts WHERE key = 'Areas/orchard.md'").fetchone()[0]
        assert "\n" not in stemmed
    finally:
        index.close()


def test_the_same_languages_keep_the_index(vault: Vault):
    path = vault.index.path
    count = len(vault.index.all_notes())
    vault.close()
    index = Index(path, vault.backend, ("sv", "en"))
    try:
        assert len(index.all_notes()) == count
    finally:
        index.close()
