from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

from mindin.cli import main
from mindin.server import VERSION

ROOT = Path(__file__).resolve().parents[1]


def shim() -> dict:
    return tomllib.loads((ROOT / "shim" / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_the_former_name_runs_mindins_command():
    project = shim()
    assert project["name"] == "notes-vault-mcp"
    target = metadata.EntryPoint("notes-vault-mcp", project["scripts"]["notes-vault-mcp"], "console_scripts")
    assert target.load() is main


def test_the_former_name_installs_mindin_of_its_own_version():
    project = shim()
    root = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == root["version"] == VERSION
    assert project["dependencies"] == [f"mindin=={VERSION}"]
    assert project["optional-dependencies"] == {"semantic": [f"mindin[semantic]=={VERSION}"]}


def test_mindin_installs_its_own_command():
    scripts = {point.name: point.value for point in metadata.entry_points(group="console_scripts")}
    assert scripts["mindin"] == "mindin.cli:main"
