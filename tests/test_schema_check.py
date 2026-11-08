from __future__ import annotations

import json
from pathlib import Path

import pytest

from mindin.cli import main
from mindin.presets import PRESETS
from mindin.schema import capabilities, schema_from_text

TEMPLATES = Path(__file__).resolve().parents[1] / "mindin" / "templates" / "presets"
BROKEN = "preset: default\nfolders:\n  Inbox: { kind: todo }\n"
ON = {
    "default": {"tasks": True, "log": True, "archive": "Archive", "systems": True},
    "para": {"tasks": True, "log": True, "archive": "Archive", "systems": True},
    "simple": {"tasks": True, "log": False, "archive": None, "systems": False},
    "journal": {"tasks": True, "log": True, "archive": "Archive", "systems": False},
    "custom": {"tasks": False, "log": False, "archive": None, "systems": False},
    "shared": {"tasks": False, "log": False, "archive": "Archive", "systems": True},
    "wiki": {"tasks": False, "log": False, "archive": "Archive", "systems": True},
}


def preset_file(name: str) -> Path:
    return TEMPLATES / name / "schema.yml"


def test_every_shipped_preset_is_covered():
    assert set(ON) == set(PRESETS)


@pytest.mark.parametrize("name", sorted(ON))
def test_the_summary_says_what_each_preset_supports(name: str):
    expected = ON[name]
    found = {c.tool: c for c in capabilities(schema_from_text(preset_file(name).read_text(encoding="utf-8")))}
    assert found["backlog_add, backlog and task open"].on is expected["tasks"]
    assert found["log_append and the stop hook's commit check"].on is expected["log"]
    archive = expected["archive"]
    kept = "keeps notes in place, no archive folder"
    assert found["close"].reason == (f"moves notes to {archive}" if archive else kept)
    assert found["session start"].on is expected["systems"]


def test_a_schema_file_without_problems_exits_zero(capsys):
    assert main(["schema", "check", str(preset_file("default"))]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "no problems; layout default"
    assert "backlog_add, backlog and task open: on (task folder Projects)" in out
    assert "log_append and the stop hook's commit check: on (log folder Log)" in out
    assert "close: on (moves notes to Archive)" in out
    assert "session start: on (loads system notes from Areas)" in out
    assert out[-1] == "session start: on (loads system notes from Areas)"


def test_the_empty_custom_layout_says_what_is_off(capsys):
    assert main(["schema", "check", str(preset_file("custom"))]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "folders: none" in out
    assert "backlog_add, backlog and task open: off (no task folder)" in out
    assert "log_append and the stop hook's commit check: off (no log folder)" in out
    assert "session start: off (loads no system notes)" in out
    assert out[-1] == "session start: off (loads no system notes)"


def test_a_schema_with_problems_lists_them_and_exits_one(tmp_path: Path, capsys):
    edited = tmp_path / "schema.yml"
    edited.write_text(BROKEN, encoding="utf-8")
    assert main(["schema", "check", str(edited)]) == 1
    out = capsys.readouterr().out
    assert "problems, so the vault runs on the default layout until they are fixed:" in out
    assert "- folders.Inbox.kind 'todo' is not one of: system, reference, task, log, archive" in out


def test_json_carries_the_problems_and_the_summary(tmp_path: Path, capsys):
    edited = tmp_path / "schema.yml"
    edited.write_text(BROKEN, encoding="utf-8")
    assert main(["schema", "check", str(edited), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["layout"] == "default" and len(report["problems"]) == 1
    assert {"name": "Projects", "kind": "task"} in report["folders"]
    assert {"tool": "close", "on": True, "reason": "moves notes to Archive"} in report["capabilities"]


def test_a_missing_file_is_a_usage_error(tmp_path: Path, capsys):
    assert main(["schema", "check", str(tmp_path / "nowhere.yml")]) == 2
    assert "cannot read" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["default", "para", "simple", "journal"])
def test_custom_made_from_a_layout_holds_and_matches_it(name: str):
    from mindin.provision import custom_schema

    made = schema_from_text(custom_schema(name))
    source = schema_from_text(preset_file(name).read_text(encoding="utf-8"))
    assert made.problems == () and made.preset == "custom" and made.made_from == name
    assert list(made.folders) == list(source.folders) and made.kind_values == source.kind_values


def test_made_from_must_be_a_name():
    assert (
        "made_from must be the name of the layout the schema was copied from"
        in schema_from_text("preset: custom\nmade_from: 3\n").problems
    )
