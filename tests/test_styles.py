from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.client import Client
from test_client_contract import Place, isolated, place, place_name, server  # noqa: F401
from test_templates import opened
from test_unsearched_folders import files, put

from mindin import notes, styles
from mindin.backends.local import LocalBackend
from mindin.server import build_server
from mindin.vault import SubjectVaults, Vault

DEFAULT = "<!-- style-version: 2 -->\nOne line saying what and why.\n"
OVERRIDE = "Two lines at most, in Swedish.\n"


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    opened_vault = opened(tmp_path)
    yield opened_vault
    opened_vault.close()


def test_a_vault_without_styles_has_none(vault: Vault):
    assert styles.read_style(vault.backend, "backlog") == styles.Style("backlog")


def test_the_default_applies_without_an_override(vault: Vault, tmp_path: Path):
    put(tmp_path, ".vault/styles/backlog.md", DEFAULT)
    found = styles.read_style(vault.backend, "backlog")
    assert found.fields() == {"style": DEFAULT, "style_source": "default", "style_note": None}


def test_the_override_applies_in_place_of_the_default(vault: Vault, tmp_path: Path):
    put(tmp_path, ".vault/styles/backlog.md", DEFAULT)
    put(tmp_path, ".vault/styles/backlog.override.md", OVERRIDE)
    found = styles.read_style(vault.backend, "backlog")
    assert (found.text, found.source) == (OVERRIDE, "override")
    assert found.note == (
        "The user's override .vault/styles/backlog.override.md applies in place of the default "
        ".vault/styles/backlog.md."
    )


def test_an_override_without_a_default_says_so(vault: Vault, tmp_path: Path):
    put(tmp_path, ".vault/styles/decision.override.md", OVERRIDE)
    assert styles.read_style(vault.backend, "decision").note.endswith("applies; there is no default.")


def test_writing_a_default_takes_markdown_without_frontmatter(vault: Vault, tmp_path: Path):
    answer = notes.write(vault, ".vault/styles/backlog.md", DEFAULT)
    assert answer.startswith("Written: .vault/styles/backlog.md\netag: ")
    assert (tmp_path / "vault/.vault/styles/backlog.md").read_text(encoding="utf-8") == DEFAULT


def test_an_older_version_is_refused_and_leaves_the_file(vault: Vault, tmp_path: Path):
    path = put(tmp_path, ".vault/styles/backlog.md", DEFAULT)
    for older in ("<!-- style-version: 1 -->\nOlder.\n", "Unstamped.\n"):
        with pytest.raises(styles.StyleRefused, match="holds style-version 2, newer than"):
            notes.write(vault, ".vault/styles/backlog.md", older)
    assert path.read_text(encoding="utf-8") == DEFAULT
    notes.write(vault, ".vault/styles/backlog.md", "<!-- style-version: 3 -->\nNewer.\n")
    assert styles.version_of(path.read_text(encoding="utf-8")) == 3


def test_the_same_version_leaves_the_file_as_it_is(vault: Vault, tmp_path: Path):
    edited = "<!-- style-version: 2 -->\nOne line, edited in place by the user.\n"
    path = put(tmp_path, ".vault/styles/backlog.md", edited)
    answer = notes.write(vault, ".vault/styles/backlog.md", DEFAULT)
    assert answer.startswith("Unchanged: .vault/styles/backlog.md\netag: ")
    assert answer.endswith("already at style-version 2; it was left as it is")
    assert path.read_text(encoding="utf-8") == edited


@pytest.mark.parametrize("stored", ["Mine, without a stamp.\n", "<!-- style-versio: 9 -->\nMine.\n"])
def test_a_stored_style_without_a_stamp_is_never_replaced(vault: Vault, tmp_path: Path, stored: str):
    path = put(tmp_path, ".vault/styles/backlog.md", stored)
    with pytest.raises(styles.StyleRefused, match="has no style-version stamp.*delete it first"):
        notes.write(vault, ".vault/styles/backlog.md", "<!-- style-version: 5 -->\nNewer.\n")
    assert path.read_text(encoding="utf-8") == stored


@pytest.mark.parametrize(
    "stored",
    [
        "\ufeff<!-- style-version: 4 -->\nAfter a BOM.\n",
        "\n  <!-- style-version: 4 -->\nAfter a blank line.\n",
        "---\ntitle: Backlog\n---\n\n<!-- style-version: 4 -->\nAfter frontmatter.\n",
    ],
)
def test_a_stamp_after_a_bom_blank_lines_or_frontmatter_is_found(vault: Vault, tmp_path: Path, stored: str):
    assert styles.version_of(stored) == 4
    path = put(tmp_path, ".vault/styles/backlog.md", stored)
    with pytest.raises(styles.StyleRefused, match="holds style-version 4, newer than the 3 offered"):
        notes.write(vault, ".vault/styles/backlog.md", "<!-- style-version: 3 -->\nOlder.\n")
    assert path.read_text(encoding="utf-8") == stored
    assert styles.stamped(stored, 5).count("style-version") == 1


def test_the_override_is_never_written(vault: Vault, tmp_path: Path):
    path = put(tmp_path, ".vault/styles/backlog.override.md", OVERRIDE)
    with pytest.raises(styles.StyleRefused, match="the user's own override: mindin never writes it"):
        notes.write(vault, ".vault/styles/backlog.override.md", DEFAULT)
    assert path.read_text(encoding="utf-8") == OVERRIDE
    answer = notes.write(vault, ".vault/styles/backlog.md", DEFAULT)
    assert answer.endswith("The user's override .vault/styles/backlog.override.md applies in place of it.")
    assert path.read_text(encoding="utf-8") == OVERRIDE


def test_a_style_is_read_back_with_its_etag(vault: Vault, tmp_path: Path):
    put(tmp_path, ".vault/styles/backlog.override.md", OVERRIDE)
    assert notes.read(vault, ".vault/styles/backlog.override.md").partition("\n")[2] == OVERRIDE


def test_a_style_never_gates_a_note(vault: Vault, tmp_path: Path):
    put(tmp_path, ".vault/styles/reference.md", "<!-- style-version: 1 -->\nAlways a table of three columns.\n")
    note = '---\ntitle: Saw\ntags: [orchard]\nstatus: active\nkind: reference\narea: "[[orchard]]"\n---\n\nhttps://example.com\n'
    assert notes.write(vault, "Resources/saw.md", note).startswith("Written: Resources/saw.md")


def test_the_version_stamp_replaces_the_files_own():
    assert styles.stamped("<!-- style-version: 1 -->\nText.\n", 4) == "<!-- style-version: 4 -->\nText.\n"
    assert styles.stamped("Text.\n", 4) == "<!-- style-version: 4 -->\nText.\n"


@pytest.mark.anyio
async def test_the_template_tool_carries_the_style(vault: Vault, tmp_path: Path):
    put(tmp_path, ".vault/styles/task.md", DEFAULT)
    put(tmp_path, ".vault/styles/backlog.override.md", OVERRIDE)
    async with Client(build_server(vault)) as client:
        task = json.loads((await client.call_tool("template", {"kind": "task"})).content[0].text)
        backlog = json.loads((await client.call_tool("template", {"kind": "backlog"})).content[0].text)
        trap = json.loads((await client.call_tool("template", {"kind": "trap"})).content[0].text)
        poem = await client.call_tool("template", {"kind": "poem"})
    assert (task["style"], task["style_source"], task["style_note"]) == (DEFAULT, "default", None)
    assert task["sections"][0]["key"] == "background"
    assert (backlog["style"], backlog["style_source"], backlog["sections"]) == (OVERRIDE, "override", [])
    assert backlog["style_note"].endswith("applies; there is no default.")
    assert (trap["style"], trap["style_source"]) == (None, None)
    assert poem.is_error and "has no style" in poem.content[0].text


@pytest.mark.anyio
async def test_backlog_add_answers_with_the_backlog_style(vault: Vault, tmp_path: Path):
    async with Client(build_server(vault)) as client:
        plain = await client.call_tool("backlog_add", {"title": "Saw", "area": "orchard", "line": "A new saw."})
        put(tmp_path, ".vault/styles/backlog.md", DEFAULT)
        styled = await client.call_tool("backlog_add", {"title": "Rake", "area": "orchard", "line": "A new rake."})
    assert plain.content[0].text == "Filed: Projects/orchard-saw.md"
    assert styled.content[0].text.startswith(
        "Filed: Projects/orchard-rake.md\n\nStyle for backlog notes, from the default:"
    )
    assert styled.content[0].text.endswith("One line saying what and why.\n")


def test_each_subject_vault_keeps_its_own_styles(vaults: SubjectVaults):
    with vaults.using_subject("sub-ann") as ann:
        notes.write(ann, ".vault/styles/backlog.md", DEFAULT)
    with vaults.using_subject("sub-bo") as bo:
        assert styles.read_style(bo.backend, "backlog").text is None
    with vaults.using_subject("sub-ann") as ann:
        assert styles.read_style(ann.backend, "backlog").text == DEFAULT


def test_a_style_folder_stays_out_of_the_note_set(vault: Vault, tmp_path: Path):
    put(tmp_path, ".vault/styles/backlog.md", DEFAULT)
    assert all(not entry.key.startswith(".vault/styles") for entry in LocalBackend(tmp_path / "vault").list())


def test_an_existing_vault_without_styles_is_left_as_it_is(vault: Vault, tmp_path: Path):
    before = files(tmp_path / "vault")
    assert styles.style_for(vault.backend, "task") == styles.Style("task")
    assert styles.style_for(vault.backend, None) is None
    assert files(tmp_path / "vault") == before
    assert not (tmp_path / "vault" / ".vault" / "styles").exists()
