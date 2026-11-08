from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from mcp.client import Client

from mindin import notes, provision
from mindin.backends import SCHEMA_KEY
from mindin.backends.local import LocalBackend
from mindin.presets import PRESETS, preset_data
from mindin.schema import NoHome, Schema, instructions, load_schema, schema_problems, unknown_preset
from mindin.search import search
from mindin.server import build_server
from mindin.vault import Vault

WITH_TASKS = {"default", "para", "simple", "journal"}
WITH_LOG = {"default", "para", "journal"}
NOTE = (
    '---\ntitle: Pear grafting\ntags: [demo]\nstatus: active\narea: "[[demo]]"\nteam: orchard\nauthor: Ada\n---\n\n'
    "Scion and rootstock.\n"
)


def open_preset(tmp_path: Path, preset: str | None) -> Vault:
    backend = LocalBackend(tmp_path / "vault")
    provision.initialize(backend, preset=preset)
    vault = Vault(backend, load_schema(backend), index_path=tmp_path / "cache" / "index.sqlite")
    vault.sync(force=True)
    return vault


def note_folder(schema: Schema) -> str:
    kept = [
        name
        for name, folder in schema.folders.items()
        if folder.kind in ("reference", "system") and folder.searched and not folder.statuses
    ]
    return kept[-1] if kept else "Notes"


@pytest.mark.parametrize("preset", list(PRESETS))
def test_every_preset_holds_its_own_contract(preset: str):
    assert schema_problems(preset_data(preset)) == []
    assert instructions(Schema(preset_data(preset)))


@pytest.mark.parametrize("preset", list(PRESETS))
def test_every_preset_writes_its_schema_and_views(tmp_path: Path, preset: str):
    backend = LocalBackend(tmp_path / "vault")
    result = provision.initialize(backend, preset=preset)
    assert result.preset == preset
    assert result.written == [SCHEMA_KEY, *PRESETS[preset].files]
    for name in PRESETS[preset].files:
        text = backend.get(name)[0]
        assert "{{STALE_DAYS}}" not in text
        if name.endswith(".base"):
            assert yaml.safe_load(text)["views"]
    assert provision.current_preset(backend) == preset


@pytest.mark.parametrize("preset", list(PRESETS))
def test_the_tools_work_on_every_preset(tmp_path: Path, preset: str):
    vault = open_preset(tmp_path, preset)
    schema = vault.schema
    assert schema.preset == preset and schema.problems == ()
    key = f"{note_folder(schema)}/pear-grafting.md"
    notes.write(vault, key, NOTE)
    with pytest.raises(notes.ValidationError, match="status 'someday' is not one of"):
        notes.write(vault, f"{note_folder(schema)}/later.md", "---\ntitle: Later\nstatus: someday\n---\n\nLater.\n")
    assert [row.note.key for row in search(vault.index, schema, "grafting").rows] == [key]

    if preset in WITH_TASKS:
        filed = notes.backlog_add(vault, "Try cleft grafts", "demo", "Next spring", priority="low")
        assert filed.startswith(f"{schema.backlog_folder}/")
        assert [note.key for note in notes.backlog(vault)] == [filed]
    else:
        with pytest.raises(NoHome, match=f"layout \\({preset}\\) has no task folder"):
            notes.backlog_add(vault, "Try cleft grafts", "demo", "Next spring")
        assert "has no task folder" in notes.render_backlog(notes.backlog(vault), vault)

    if preset in WITH_LOG:
        logged = notes.log_append(vault, "demo", "grafted two pears", commits=("abc1234",))
        assert f"{schema.log_folder}/demo-log.md" in logged
        assert any("grafted two pears" in line for line in notes.context(vault, repo="demo").log_tail)
    else:
        with pytest.raises(NoHome, match="has no log folder"):
            notes.log_append(vault, "demo", "grafted two pears")
        assert notes.context(vault, repo="demo").log_tail == []

    bundle = notes.context(vault, repo="demo")
    assert "schema" not in bundle.render()
    findings = notes.lint(vault).as_dict()
    assert not {"schema", "broken_frontmatter", "missing_area"} & set(findings), findings

    closed = notes.close(vault, key)
    assert closed == (f"{schema.archive_folder}/pear-grafting.md" if schema.archive_folder else key)
    vault.close()


@pytest.mark.parametrize("preset", list(PRESETS))
def test_a_fresh_vault_of_every_preset_lints_clean(tmp_path: Path, preset: str):
    vault = open_preset(tmp_path, preset)
    assert notes.lint(vault).as_dict() == {}
    vault.close()


def test_simple_keeps_notes_in_the_root_typed_by_category(tmp_path: Path):
    vault = open_preset(tmp_path, "simple")
    notes.write(vault, "Plants.md", "---\ntags: [categories]\n---\n\n![[Category.base]]\n")
    root_note = '---\ncategories: ["[[Plants]]"]\nstatus: active\n---\n\nRepot the fig.\n'
    notes.write(vault, "Fig repotting.md", root_note)
    assert [row.note.key for row in search(vault.index, vault.schema, "repot").rows] == ["Fig repotting.md"]
    assert vault.schema.folder_of("Fig repotting.md") == ""
    assert "- the root — Most notes" in instructions(vault.schema)
    assert set(notes.lint(vault).as_dict()) <= {"orphans"}
    filed = notes.backlog_add(vault, "Fig", "garden", "Repot it in spring", priority="low")
    assert filed == "Projects/garden-fig.md"
    assert [note.key for note in notes.backlog(vault)] == [filed]
    with pytest.raises(NoHome, match="layout \\(simple\\) has no log folder"):
        notes.log_append(vault, "garden", "repotted")
    assert notes.close(vault, filed) == filed
    assert "done_not_closed" not in notes.lint(vault).as_dict()
    vault.close()


def test_journal_files_days_by_year_and_work_beside_them(tmp_path: Path):
    vault = open_preset(tmp_path, "journal")
    day = "---\ntags: [daily]\nkind: day\n---\n\nPruned the pear.\n"
    notes.write(vault, "Daily/2026/2026-11-01.md", day)
    assert vault.schema.folder_of("Daily/2026/2026-11-01.md") == "Daily"
    filed = notes.backlog_add(vault, "Graft the pear", "orchard", "In spring")
    assert filed == "Projects/orchard-graft-the-pear.md"
    assert "Logs/orchard-log.md" in notes.log_append(vault, "orchard", "pruned")
    assert notes.close(vault, filed) == "Archive/orchard-graft-the-pear.md"
    vault.close()


def test_a_vault_on_a_preset_this_version_does_not_ship_is_read_as_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    from mindin.cli import main

    retired = (
        "version: 1\npreset: lyt\nfolders:\n  Efforts/On: { kind: task }\n  Calendar/Logs: { kind: log }\n"
        "  Efforts/Sleeping: { kind: archive, searched: false }\nfrontmatter:\n  required: [title]\n"
        "  status_values: [draft, active, backlog, complete, superseded]\n  kind_values: [task, log]\n"
    )
    backend = LocalBackend(tmp_path / "vault")
    backend.put(SCHEMA_KEY, retired)
    vault = Vault(backend, load_schema(backend), index_path=tmp_path / "cache" / "index.sqlite")
    vault.sync(force=True)
    assert vault.schema.problems == ()
    assert vault.schema.notices == (unknown_preset("lyt"),)
    assert set(vault.schema.folders) == {"Efforts/On", "Calendar/Logs", "Efforts/Sleeping"}
    assert notes.backlog_add(vault, "Prune", "orchard", "Soon").startswith("Efforts/On/")
    assert "Calendar/Logs/orchard-log.md" in notes.log_append(vault, "orchard", "pruned")
    assert "preset 'lyt' is not one this version ships" in notes.context(vault, repo="orchard").render()
    assert "schema" in notes.lint(vault).as_dict()
    assert provision.current_preset(backend) == "custom"
    for force in (False, True):
        result = provision.initialize(backend, force=force)
        assert (result.preset, result.written, result.kept) == ("lyt", [], [SCHEMA_KEY])
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("VAULT_SCHEMA", raising=False)
    monkeypatch.delenv("VAULT_SERVER_URL", raising=False)
    assert main(["init", "--force"]) == 0
    assert "the schema names 'lyt', which this version does not ship" in capsys.readouterr().err
    assert backend.get(SCHEMA_KEY)[0] == retired
    vault.close()


def test_a_closed_note_in_a_layout_without_an_archive_stays_put_and_lint_accepts_it(tmp_path: Path):
    vault = open_preset(tmp_path, "custom")
    notes.write(vault, "Notes/kept.md", NOTE)
    assert notes.close(vault, "Notes/kept.md") == "Notes/kept.md"
    assert "done_not_closed" not in notes.lint(vault).as_dict()
    vault.close()


def test_without_a_preset_init_keeps_the_built_in_layout(tmp_path: Path):
    backend = LocalBackend(tmp_path / "vault")
    assert provision.initialize(backend).preset == "default"
    assert backend.get(SCHEMA_KEY)[0] == provision.template("presets/default/schema.yml")
    assert load_schema(backend).folders.keys() == {"Areas", "Resources", "Projects", "Log", "Archive"}


def test_init_without_a_preset_rewrites_the_vaults_own_layout(tmp_path: Path):
    backend = LocalBackend(tmp_path / "vault")
    provision.initialize(backend, preset="journal")
    assert provision.initialize(backend, force=True).preset == "journal"
    assert load_schema(backend).log_folder == "Logs"


def test_switching_preset_keeps_every_note_and_saves_the_old_schema(tmp_path: Path):
    vault = open_preset(tmp_path, None)
    notes.write(vault, "Areas/orchard.md", NOTE)
    notes.write(vault, "Projects/orchard-pruning.md", NOTE)
    notes.log_append(vault, "orchard", "pruned")
    backend = vault.backend
    backend.put("Open tasks.base", backend.get("Open tasks.base")[0] + "# mine\n")
    before = {entry.key: backend.get(entry.key)[0] for entry in backend.list() if entry.key.endswith(".md")}
    old_schema = backend.get(SCHEMA_KEY)[0]

    result = provision.initialize(backend, force=True, preset="para")

    after = {entry.key: backend.get(entry.key)[0] for entry in backend.list() if entry.key.endswith(".md")}
    assert after == before
    assert backend.get(provision.PREVIOUS_SCHEMA_KEY)[0] == old_schema
    assert provision.current_preset(backend) == "para"
    assert result.removed == []
    assert "# mine" in backend.get("Open tasks.base")[0]
    vault.sync(force=True)
    assert vault.schema.preset == "para"
    again = provision.initialize(backend, force=True, preset="simple")
    assert set(again.removed) == {"Projects.base", "Areas.base", "Resources.base", "Log.base"}
    assert "Backlog.base" in again.written
    assert "Templates/Category Template.md" in again.written
    assert {row.note.key for row in search(vault.index, vault.schema, "grafting").rows} >= set(before) - {
        "Log/orchard-log.md"
    }
    vault.close()


def test_a_broken_schema_is_named_with_its_line_and_the_vault_runs_on_its_preset(tmp_path: Path):
    vault = open_preset(tmp_path, "simple")
    vault.backend.put(SCHEMA_KEY, "version: 1\npreset: simple\nfolders:\n  Inbox: { kind: task\n")
    vault.reload_schema()
    assert vault.schema.preset == "simple"
    assert vault.schema.problems and vault.schema.problems[0].startswith("line ")
    rendered = notes.context(vault, repo="demo").render()
    assert rendered.startswith(f"## schema\n{SCHEMA_KEY} does not hold, so the vault runs on the simple layout")
    assert "schema" in notes.lint(vault).as_dict()
    vault.close()


def test_a_custom_schema_with_wrong_values_says_each_one_readably(tmp_path: Path):
    backend = LocalBackend(tmp_path / "vault")
    provision.initialize(backend, preset="custom")
    backend.put(
        SCHEMA_KEY,
        "preset: custom\n"
        "folders:\n"
        "  Work: { kind: tasks }\n"
        "  Work/Logs: { kind: log, weight: heavy }\n"
        "  ../Escape: { kind: reference }\n"
        "tags: { strict: maybe }\n"
        "log: { file_format: '{name}.md' }\n"
        "stale_after_days: soon\n",
    )
    problems = load_schema(backend).problems
    assert problems == (
        "folders.Work.kind 'tasks' is not one of: system, reference, task, log, archive",
        "folders.Work/Logs.weight must be a number, 0 or more",
        "folders.../Escape: a folder is a vault path like Notes or Work/Logs, without dot segments",
        "tags.strict must be true or false",
        "stale_after_days must be a whole number of days, 1 or more",
        "log.file_format must contain {repo}",
    )


def test_a_layout_that_the_tools_cannot_write_into_is_refused(tmp_path: Path):
    backend = LocalBackend(tmp_path / "vault")
    backend.put(
        SCHEMA_KEY,
        "preset: custom\nfolders:\n  Todo: { kind: task }\nfrontmatter:\n  status_values: [open, done]\n"
        "  area_required_in: [Elsewhere]\n",
    )
    assert load_schema(backend).problems == (
        "frontmatter.status_values lacks complete, superseded, active, backlog, which close, backlog_add and "
        "set_status write",
        "frontmatter.kind_values lacks task, which the tools write in the task folder",
        "frontmatter.area_required_in names 'Elsewhere', which is not one of the folders",
    )


def test_a_custom_layout_built_from_empty_gets_every_tool(tmp_path: Path):
    vault = open_preset(tmp_path, "custom")
    vault.backend.put(
        SCHEMA_KEY,
        "preset: custom\n"
        "folders:\n"
        "  Work: { kind: task }\n"
        "  Work/Logs: { kind: log }\n"
        "  Done: { kind: archive, searched: false }\n"
        "frontmatter:\n  kind_values: [task, log]\n",
    )
    vault.reload_schema()
    assert vault.schema.problems == ()
    assert notes.backlog_add(vault, "Fence", "demo", "Mend it").startswith("Work/")
    assert "Work/Logs/demo-log.md" in notes.log_append(vault, "demo", "mended")
    assert [note.key for note in notes.backlog(vault)] == ["Work/demo-fence.md"]
    vault.close()


def test_a_dropped_folder_leaves_the_preset(tmp_path: Path):
    backend = LocalBackend(tmp_path / "vault")
    backend.put(
        SCHEMA_KEY,
        "preset: para\nfolders:\n  Resources: null\n  Resources/Logs: null\nlog: { folder: null }\n"
        "frontmatter:\n  area_required_in: [Projects]\n",
    )
    schema = load_schema(backend)
    assert schema.problems == ()
    assert "Resources" not in schema.folders and schema.log_folder is None


def test_nested_folders_decide_kind_weight_and_search_scope(tmp_path: Path):
    vault = open_preset(tmp_path, "para")
    schema = vault.schema
    assert schema.folder_of("Resources/Logs/deep/x.md") == "Resources/Logs"
    assert schema.folder_of("Resources/x/y.md") == "Resources"
    assert schema.folder_of("Loose/x.md") == "Loose"
    notes.write(vault, "Archive/old-pear.md", NOTE)
    notes.write(vault, "Areas/pear.md", NOTE)
    notes.write(vault, "Resources/Logs/pear-log.md", NOTE)
    shown = search(vault.index, schema, "grafting")
    assert {row.note.key for row in shown.rows} == {"Areas/pear.md", "Resources/Logs/pear-log.md"}
    assert shown.hidden_archive == 1
    scoped = search(vault.index, schema, "grafting", folder="Resources")
    assert [row.note.key for row in scoped.rows] == ["Resources/Logs/pear-log.md"]
    vault.close()


def test_a_schema_changed_on_disk_is_picked_up_by_the_running_vault(tmp_path: Path):
    vault = open_preset(tmp_path, None)
    vault.backend.put(SCHEMA_KEY, provision.template("presets/simple/schema.yml"))
    vault.index.set_meta("last_sync", "0")
    vault.sync()
    assert vault.schema.preset == "simple"
    with pytest.raises(NoHome):
        notes.log_append(vault, "demo", "a line")
    vault.close()


@pytest.mark.anyio
async def test_the_init_tool_sets_up_a_vault_without_a_schema_in_the_preset_asked_for(tmp_path: Path):
    backend = LocalBackend(tmp_path / "vault")
    vault = Vault(backend, load_schema(backend), index_path=tmp_path / "cache" / "index.sqlite")
    async with Client(build_server(vault)) as client:
        unknown = await client.call_tool("init", {"preset": "bullet-journal"})
        made = await client.call_tool("init", {"preset": "journal"})
        again = await client.call_tool("init", {"preset": "journal"})
        logged = await client.call_tool("log_append", {"repo": "demo", "line": "a line"})
    assert unknown.is_error and "the presets are: default, para" in unknown.content[0].text
    assert made.content[0].text.startswith("layout: journal\nwrote .vault/schema.yml")
    assert not again.is_error and "kept .vault/schema.yml" in again.content[0].text
    assert "Logs/demo-log.md" in logged.content[0].text
    vault.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [
        {"preset": "simple"},
        {"preset": "simple", "force": True},
        {"force": True},
        {"preset": "default", "force": True},
    ],
)
async def test_an_mcp_client_cannot_switch_or_reset_an_existing_vaults_layout(
    vault: Vault, vault_dir: Path, arguments: dict
):
    before = (vault_dir / ".vault" / "schema.yml").read_bytes()
    async with Client(build_server(vault)) as client:
        refused = await client.call_tool("init", arguments)
        logged = await client.call_tool("log_append", {"repo": "orchard", "line": "a line"})
    assert refused.is_error
    assert "already has a layout (default)" in refused.content[0].text
    assert "init --preset" in refused.content[0].text and "account page" in refused.content[0].text
    assert (vault_dir / ".vault" / "schema.yml").read_bytes() == before
    assert not (vault_dir / ".vault" / "schema.previous.yml").exists()
    assert vault.schema.preset == "default" and not logged.is_error


@pytest.mark.anyio
async def test_write_file_refuses_the_schema_and_says_how_to_change_it(vault: Vault):
    async with Client(build_server(vault)) as client:
        refused = await client.call_tool("write_file", {"path": SCHEMA_KEY, "content": "preset: simple\n"})
        readable = await client.call_tool("read_file", {"path": SCHEMA_KEY})
    assert refused.is_error and "edit it as a file" in refused.content[0].text
    assert not readable.is_error


def test_cli_init_preset_switches_with_force_and_keeps_without(vault: Vault, vault_dir: Path, capsys):
    from mindin.cli import main

    assert main(["init", "--preset", "journal"]) == 0
    assert provision.preset_of((vault_dir / ".vault" / "schema.yml").read_text()) == "default"
    assert main(["init", "--preset", "journal", "--force"]) == 0
    err = capsys.readouterr().err
    assert "layout: journal" in err and "No note was moved or deleted." in err
    assert provision.preset_of((vault_dir / ".vault" / "schema.yml").read_text()) == "journal"
    assert (vault_dir / "Days.base").exists() and (vault_dir / "Areas" / "orchard.md").exists()
    with pytest.raises(SystemExit):
        main(["init", "--preset", "bullet-journal"])
