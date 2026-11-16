from __future__ import annotations

from pathlib import Path

from test_templates import opened

from mindin.schema import instructions, schema_problems
from mindin.search import search

NOTE = '---\ntitle: {title}\ntags: [orchard]\nstatus: active\narea: "[[orchard]]"\n---\n\nPrune the {title} trees.\n'


def put(tmp_path: Path, key: str, text: str) -> Path:
    path = tmp_path / "vault" / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def files(folder: Path) -> dict[str, bytes]:
    return {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob("*") if path.is_file()}


def test_the_schema_names_folders_search_skips(tmp_path: Path):
    vault = opened(tmp_path, "unsearched_folders: [Kebbie, Work/Cards]\n")
    try:
        for key, title in (("Kebbie/card.md", "pear"), ("Work/Cards/card.md", "pear"), ("Resources/pear.md", "pear")):
            put(tmp_path, key, NOTE.format(title=title))
        vault.index.sync(force=True)
        found = search(vault.index, vault.schema, "prune pear trees")
        assert [row.note.key for row in found.rows] == ["Resources/pear.md"] and found.hidden_archive == 2
        every = search(vault.index, vault.schema, "prune pear trees", include_archive=True)
        assert len(every.rows) == 3
        assert "- Kebbie/ — not searched by default" in instructions(vault.schema)
    finally:
        vault.close()


def test_a_folder_search_skips_must_be_a_vault_path():
    assert schema_problems({"unsearched_folders": [".hidden"]}) == [
        "unsearched_folders: '.hidden' is not a vault path like Kebbie or Work/Logs"
    ]
    assert schema_problems({"unsearched_folders": "Kebbie"}) == [
        "unsearched_folders must be a list of words, like [a, b]"
    ]


def test_an_existing_vault_reads_and_searches_as_before(tmp_path: Path):
    vault = opened(tmp_path)
    try:
        put(tmp_path, "Kebbie/card.md", NOTE.format(title="pear"))
        before = files(tmp_path / "vault")
        vault.index.sync(force=True)
        assert [row.note.key for row in search(vault.index, vault.schema, "prune pear trees").rows] == [
            "Kebbie/card.md"
        ]
        assert vault.schema.unsearched_folders == ["Archive"]
        assert files(tmp_path / "vault") == before
    finally:
        vault.close()
