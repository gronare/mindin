from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from mcp.client import Client

from mindin import note_templates, notes, provision
from mindin.backends.local import LocalBackend
from mindin.cli import main
from mindin.frontmatter import parse
from mindin.presets import preset_data
from mindin.schema import Schema, instructions, load_schema, schema_from_text, schema_problems
from mindin.server import build_server
from mindin.vault import Vault

AWS_KEY = "AKIA" + "ZQ3XJ7QZPL3MNB6V"
TASK = '---\ntitle: Prune\ntags: [orchard]\nstatus: {status}\nkind: task\narea: "[[orchard]]"\n---\n\n{body}'
SYSTEM = "---\ntitle: Orchard\ntags: [orchard]\nstatus: active\nkind: system\n---\n\n{body}"
WRITTEN_TASK = (
    "## Background\nThe low branches shade the beds.\n\n## Goal\nLight on the beds by May.\n\n## What we know\n\n"
    "## Open questions\n- [ ] Which saw?\n\n## Out of scope\n\n## Source\nA walk on 2026-11-01.\n"
)
TASK_SECTIONS = [
    {"key": "background", "heading": "Background", "required": True},
    {"key": "goal", "heading": "Goal", "required": True},
    {"key": "known", "heading": "What we know", "required": False},
    {"key": "questions", "heading": "Open questions", "required": False},
    {"key": "scope", "heading": "Out of scope", "required": False},
    {"key": "source", "heading": "Source", "required": True},
]
TASK_SKELETON = "## Background\n\n## Goal\n\n## What we know\n\n## Open questions\n\n## Out of scope\n\n## Source\n"


def opened(tmp_path: Path, extra: str = "") -> Vault:
    backend = LocalBackend(tmp_path / "vault")
    provision.initialize(backend)
    if extra:
        schema = tmp_path / "vault" / ".vault" / "schema.yml"
        schema.write_text(schema.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return Vault(backend, load_schema(backend), index_path=tmp_path / "index.sqlite")


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    opened_vault = opened(tmp_path)
    yield opened_vault
    opened_vault.close()


def stored(tmp_path: Path, key: str) -> tuple[dict, str]:
    return parse((tmp_path / "vault" / key).read_text(encoding="utf-8"))


def test_the_default_layout_carries_a_task_and_a_system_template():
    schema = Schema(preset_data("default"))
    task, system = schema.template("task"), schema.template("system")
    assert task.keys == ["background", "goal", "known", "questions", "scope", "source"]
    assert task.required_keys == ["background", "goal", "source"] and task.questions == "questions"
    assert system.keys == ["what", "objects", "flows", "limits", "exceptions", "traps", "left", "history"]
    assert system.required_keys == ["what"] and system.questions is None
    assert [section.heading("sv") for section in system.sections][:2] == ["Vad det gör", "Objekten som bär sanningen"]
    assert schema.template("trap") is None


def test_every_shipped_layout_holds_its_templates():
    for name in ("default", "para", "simple", "journal", "custom", "shared", "wiki"):
        assert schema_problems(preset_data(name)) == [], name


@pytest.mark.parametrize(
    ("templates", "problem"),
    [
        ("templates: { task: [a] }", "templates.task must be a mapping like"),
        ("templates: { task: { sections: [] } }", "templates.task.sections must be a list of sections"),
        ("templates: { task: { sections: [{ key: Goal, heading: Goal }] } }", "sections[0].key must be a lowercase"),
        (
            "templates: { task: { sections: [{ key: goal, heading: G }, { key: goal, heading: H }] } }",
            "sections[1].key goal is already a section of templates.task",
        ),
        ("templates: { task: { sections: [{ key: goal, heading: { en: 3 } }] } }", "sections[0].heading must be"),
        ("templates: { task: { sections: [{ key: goal, heading: G, required: yes please }] } }", "required must be"),
        ("templates: { task: { sections: [{ key: goal, heading: G, note: x }] } }", "a section takes key, heading"),
        ("templates: { task: { sections: [{ key: goal, heading: G }], questions: why } }", "questions must name"),
        ("templates: { task: { sections: [{ key: goal, heading: G }], order: 1 } }", "a template takes sections"),
        ("task: { sections: { en: [Goal] } }", "task.sections.en must map section keys to headings"),
        ("task: { sections: { sv: null } }", "task.sections.sv must map section keys to headings"),
        ("task: { sections: { en: { problem: 'Why #' } } }", "task.sections.en has a heading on more than one"),
        ("templates: { task: { sections: [{ key: goal, heading: 'C#' }] } }", "does not end in #"),
        ('templates: { task: { sections: [{ key: goal, heading: { en: "A\\nB" } }] } }', "must be one line"),
    ],
)
def test_a_template_that_does_not_hold_is_named(templates: str, problem: str):
    schema = schema_from_text(templates)
    assert any(problem in found for found in schema.problems), schema.problems
    assert schema.template("task").keys[0] == "background", "the vault keeps its layout's template"


def test_a_stored_task_sections_key_becomes_the_task_template():
    schema = schema_from_text("language: sv\ntask:\n  sections:\n    sv: { problem: Varför }\n")
    task = schema.template("task")
    assert task.keys == ["problem", "solution", "decisions", "tests", "scope"]
    assert task.required_keys == ["problem", "solution", "decisions"] and task.questions is None
    assert [section.heading("sv") for section in task.sections][:2] == ["Varför", "Lösning"]
    assert schema.template("system").keys[0] == "what"


def test_a_stored_task_template_wins_over_the_old_sections_key():
    text = "task: { sections: { en: { problem: Problem } } }\n"
    text += "templates: { task: { sections: [{ key: why, heading: Why }] } }"
    assert schema_from_text(text).template("task").keys == ["why"]


def test_a_template_for_a_kind_the_vault_drops_is_ignored():
    schema = schema_from_text("kinds: { system: null }\nfrontmatter: { kind_values: [task, log] }\n")
    assert schema.problems == () and schema.template("system") is None


def test_write_refuses_an_active_task_without_its_headings(vault: Vault, tmp_path: Path):
    with pytest.raises(notes.ValidationError) as caught:
        notes.write(vault, "Projects/orchard-prune.md", TASK.format(status="active", body="Cut the branches.\n"))
    message = str(caught.value)
    assert message.startswith("Projects/orchard-prune.md does not follow the task template:\n- missing headings: ")
    assert "## Background, ## Goal, ## What we know, ## Open questions, ## Out of scope, ## Source" in message
    assert "The task template (* is required): task: ## Background*, ## Goal*, ## What we know" in message
    assert "`- [ ]` marks a question that blocks the start" in message
    assert not (tmp_path / "vault" / "Projects" / "orchard-prune.md").exists()


def test_write_refuses_a_required_section_left_empty(vault: Vault):
    body = WRITTEN_TASK.replace("Light on the beds by May.\n", "")
    with pytest.raises(notes.ValidationError, match=r"- required sections left empty: ## Goal\n"):
        notes.write(vault, "Projects/orchard-prune.md", TASK.format(status="active", body=body))


def test_write_accepts_a_written_out_task_and_one_just_opened(vault: Vault):
    assert notes.write(vault, "Projects/a.md", TASK.format(status="active", body=WRITTEN_TASK)).startswith("Written")
    assert notes.write(vault, "Projects/b.md", TASK.format(status="active", body=TASK_SKELETON)).startswith("Written")


def test_a_backlog_or_draft_note_is_a_draft_held_to_nothing(vault: Vault):
    notes.write(vault, "Projects/later.md", TASK.format(status="backlog", body="One line for later.\n"))
    notes.write(vault, "Projects/drafting.md", TASK.format(status="draft", body="Half a thought.\n"))
    notes.append(vault, "Projects/later.md", "Another line.")


def test_a_system_note_is_held_to_its_template_and_takes_headings_in_any_language(vault: Vault):
    with pytest.raises(notes.ValidationError, match="does not follow the system template"):
        notes.write(vault, "Areas/orchard.md", SYSTEM.format(body="It grows pears.\n"))
    swedish = "## Vad det gör\nOdlar päron.\n\n## Objekten som bär sanningen\n\n## Flödena\n\n## Gränser\n\n"
    swedish += "## Undantag\n\n## Fällor\n\n## Kvar\n\n## Historik\n"
    assert notes.write(vault, "Areas/orchard.md", SYSTEM.format(body=swedish)).startswith("Written")


def old_note(tmp_path: Path) -> str:
    (tmp_path / "vault" / "Areas").mkdir(exist_ok=True)
    (tmp_path / "vault" / "Areas" / "old.md").write_text(SYSTEM.format(body="Free text.\n"), encoding="utf-8")
    return "Areas/old.md"


def test_a_note_from_before_its_template_takes_appends_and_rewrites(vault: Vault, tmp_path: Path):
    key = old_note(tmp_path)
    notes.append(vault, key, "- 2026-11-12 abc1234 one more line")
    assert notes.write(vault, key, SYSTEM.format(body="Still free text.\n")).startswith("Written")
    assert stored(tmp_path, key)[1].strip() == "Still free text."


def test_a_note_that_follows_its_template_refuses_a_rewrite_that_breaks_it(vault: Vault, tmp_path: Path):
    notes.write(vault, "Projects/a.md", TASK.format(status="active", body=WRITTEN_TASK))
    notes.append(vault, "Projects/a.md", "- A note on the saw.")
    assert stored(tmp_path, "Projects/a.md")[1].rstrip().endswith("A walk on 2026-11-01.\n- A note on the saw.")
    dropped = WRITTEN_TASK.replace("Light on the beds by May.\n", "")
    with pytest.raises(notes.ValidationError, match=r"- required sections left empty: ## Goal\n"):
        notes.write(vault, "Projects/a.md", TASK.format(status="active", body=dropped))
    etag = notes.read(vault, "Projects/a.md").splitlines()[0].removeprefix("etag: ")
    with pytest.raises(notes.ValidationError, match="missing headings: ## Background"):
        notes.write(vault, "Projects/a.md", TASK.format(status="active", body="Gone.\n"), expected_etag=etag)
    assert "Light on the beds by May." in stored(tmp_path, "Projects/a.md")[1]


def test_a_heading_inside_a_fence_does_not_count(vault: Vault):
    fenced = WRITTEN_TASK.replace("## Goal\nLight on the beds by May.\n", "```\n## Goal\nLight.\n```\n")
    with pytest.raises(notes.ValidationError, match="missing headings: ## Goal"):
        notes.write(vault, "Projects/a.md", TASK.format(status="active", body=fenced))
    assert note_templates.filled_headings("~~~\n# Inside\n~~~\n# Outside\nText.\n") == {"outside": True}


def test_a_long_heading_line_is_read_in_linear_time():
    line = "# x" + " " * 200_000 + "y #"
    started = time.perf_counter()
    assert note_templates.filled_headings(f"{line}\nText.\n") == {"x" + " " * 200_000 + "y": True}
    assert time.perf_counter() - started < 0.5
    assert note_templates.heading_of("####### Seven") is None and note_templates.heading_of("#Tight") is None
    assert note_templates.heading_of("## Goal ##") == (2, "Goal")


def test_a_stored_note_with_broken_frontmatter_can_be_rewritten(vault: Vault, tmp_path: Path):
    (tmp_path / "vault" / "Projects").mkdir(exist_ok=True)
    broken = "---\ntitle: [unclosed\n---\n\nText.\n"
    (tmp_path / "vault" / "Projects" / "broken.md").write_text(broken, encoding="utf-8")
    written = notes.write(vault, "Projects/broken.md", TASK.format(status="active", body="Mended.\n"))
    assert written.startswith("Written") and stored(tmp_path, "Projects/broken.md")[0]["title"] == "Prune"


def test_a_draft_picked_up_is_held_to_its_template(vault: Vault, tmp_path: Path):
    key = notes.backlog_add(vault, "Prune", "orchard", "The beds need light.")
    notes.set_status(vault, key, "active")
    with pytest.raises(notes.ValidationError, match="missing headings: ## Background"):
        notes.write(vault, key, TASK.format(status="active", body="Cut the branches.\n"))
    _, drafted = stored(tmp_path, key)
    with pytest.raises(notes.ValidationError, match="the backlog's draft line is still there"):
        notes.write(vault, key, TASK.format(status="active", body=drafted.replace("## Goal\n", "## Goal\nLight.\n")))
    notes.write(vault, "Projects/drafting.md", TASK.format(status="draft", body="Half a thought.\n"))
    with pytest.raises(notes.ValidationError, match="does not follow the task template"):
        notes.write(vault, "Projects/drafting.md", TASK.format(status="active", body="Half a thought.\n"))
    assert notes.write(vault, key, TASK.format(status="active", body=WRITTEN_TASK)).startswith("Written")


def test_a_rewrite_that_changes_or_drops_the_kind_is_held_to_the_template(vault: Vault, tmp_path: Path):
    (tmp_path / "vault" / "Projects").mkdir(exist_ok=True)
    trap = TASK.format(status="active", body="Free text.\n").replace("kind: task", "kind: trap")
    (tmp_path / "vault" / "Projects" / "old.md").write_text(trap, encoding="utf-8")
    with pytest.raises(notes.ValidationError, match="does not follow the task template"):
        notes.write(vault, "Projects/old.md", TASK.format(status="active", body="Free text.\n"))
    notes.write(vault, "Projects/a.md", TASK.format(status="active", body=WRITTEN_TASK))
    kindless = TASK.format(status="active", body="Gone.\n").replace("kind: task\n", "")
    with pytest.raises(notes.ValidationError, match="does not follow the task template"):
        notes.write(vault, "Projects/a.md", kindless)


def test_lint_lists_open_notes_that_do_not_follow_their_template(vault: Vault, tmp_path: Path):
    old_note(tmp_path)
    notes.write(vault, "Projects/a.md", TASK.format(status="active", body=WRITTEN_TASK))
    notes.write(vault, "Projects/later.md", TASK.format(status="backlog", body="One line.\n"))
    vault.index.sync(force=True)
    assert notes.lint(vault).as_dict()["off_template"] == [
        "Areas/old.md: does not follow the system template: missing headings: ## What it does, "
        "## The objects that hold the truth, ## Flows, ## Limits, ## Exceptions, ## Traps, ## Left, ## History"
    ]


def test_backlog_add_with_a_line_files_a_draft_of_the_template(vault: Vault, tmp_path: Path):
    key = notes.backlog_add(vault, "Prune", "orchard", "The beds need light.", priority="low", source="a walk")
    frontmatter, body = stored(tmp_path, key)
    assert frontmatter["status"] == "backlog" and frontmatter["summary"] == "The beds need light."
    assert body == (
        f"\n## Background\n{note_templates.DRAFT_MARKER}\n\n## Goal\n\n## What we know\n\n## Open questions\n\n"
        "## Out of scope\n\n## Source\nThe beds need light.\na walk\n"
    )


def test_backlog_add_with_sections_writes_them_in_template_order(vault: Vault, tmp_path: Path):
    sections = {"source": "A walk.", "goal": "Light on the beds.\nBy May.", "background": "Shade.", "scope": "Pears."}
    key = notes.backlog_add(vault, "Prune", "orchard", sections=sections)
    frontmatter, body = stored(tmp_path, key)
    assert frontmatter["status"] == "backlog" and frontmatter["summary"] == "Light on the beds."
    assert body == (
        "\n## Background\nShade.\n\n## Goal\nLight on the beds.\nBy May.\n\n## What we know\n\n## Open questions\n\n"
        "## Out of scope\nPears.\n\n## Source\nA walk.\n"
    )


def test_backlog_add_refuses_sections_that_miss_the_template(vault: Vault):
    with pytest.raises(ValueError, match=r"needs text in goal \(## Goal\), source \(## Source\)"):
        notes.backlog_add(vault, "Prune", "orchard", sections={"background": "Shade."})
    with pytest.raises(ValueError, match="unknown section why; the task template has background, goal"):
        notes.backlog_add(vault, "Prune", "orchard", sections={"why": "Shade."})
    with pytest.raises(ValueError, match="needs line"):
        notes.backlog_add(vault, "Prune", "orchard")


def test_backlog_add_runs_the_guard_write_file_runs(tmp_path: Path):
    guarded = opened(tmp_path, "bundle:\n  guard:\n    secrets: true\n")
    try:
        with pytest.raises(notes.ValidationError, match="an AWS access key"):
            notes.backlog_add(guarded, "Rotate", "orchard", f"The key {AWS_KEY} leaked.")
        assert not list((tmp_path / "vault" / "Projects").glob("*.md"))
    finally:
        guarded.close()


def test_the_instructions_list_each_template_with_its_required_sections(vault: Vault):
    text = instructions(vault.schema)
    assert (
        "- task: ## Background*, ## Goal*, ## What we know, ## Open questions, ## Out of scope, ## Source*. "
        "Under ## Open questions, `- [ ]` marks a question that blocks the start and `- ` one the work settles"
    ) in text
    assert "- system: ## What it does*, ## The objects that hold the truth, ## Flows, ## Limits" in text
    assert "a backlog or draft note is a draft, held to nothing" in text
    assert "Note templates" not in instructions(Schema(preset_data("custom")))


@pytest.mark.anyio
async def test_the_template_tool_answers_a_kind_and_every_kind(vault: Vault):
    async with Client(build_server(vault)) as client:
        one = await client.call_tool("template", {"kind": "task"})
        every = await client.call_tool("template", {})
        none = await client.call_tool("template", {"kind": "trap"})
        unknown = await client.call_tool("template", {"kind": "poem"})
        rules = await client.call_tool("rules", {})
    task = json.loads(one.content[0].text)
    assert task == {
        "kind": "task",
        "language": "en",
        "sections": TASK_SECTIONS,
        "questions": "questions",
        "skeleton": TASK_SKELETON,
    }
    answer = json.loads(every.content[0].text)
    assert answer["language"] == "en" and list(answer["templates"]) == ["task", "system"]
    assert answer["templates"]["task"] == task
    assert json.loads(none.content[0].text)["sections"] == []
    assert unknown.is_error and "kind 'poem' is not one of" in unknown.content[0].text
    assert "- task: ## Background*" in rules.content[0].text


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forget_storage: Callable[[], None]) -> Path:
    (tmp_path / "claude").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("VAULT_SCHEMA", raising=False)
    forget_storage()
    provision.initialize(LocalBackend(tmp_path / "vault"))
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    return tmp_path / "vault"


def test_the_template_command_answers_the_same_json(configured: Path, capsys):
    assert main(["template", "--json", "--kind", "task"]) == 0
    assert json.loads(capsys.readouterr().out)["sections"] == TASK_SECTIONS
    assert main(["template", "--json"]) == 0
    assert list(json.loads(capsys.readouterr().out)["templates"]) == ["task", "system"]
    assert main(["template"]) == 0
    assert capsys.readouterr().out.splitlines()[1].startswith("system: ## What it does*, ## The objects")
    assert main(["template", "--json", "--kind", "poem"]) == 1
    assert "kind 'poem' is not one of" in capsys.readouterr().err
