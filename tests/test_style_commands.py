from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_client_contract import Place, isolated, place, place_name, server  # noqa: F401
from test_styles import OVERRIDE

from mindin.cli import main


def test_the_cli_writes_and_shows_the_style_locally_and_on_a_server(place: Place, tmp_path: Path, capsys):  # noqa: F811
    source = tmp_path / "backlog-style.md"
    source.write_text("One line saying what and why.\n", encoding="utf-8")
    assert main(["style", "write", "--kind", "backlog", "--file", str(source), "--version", "3"]) == 0
    written = json.loads(capsys.readouterr().out)
    assert (written["path"], written["version"]) == (".vault/styles/backlog.md", 3)
    assert place.read(".vault/styles/backlog.md") == "<!-- style-version: 3 -->\nOne line saying what and why.\n"
    assert main(["style", "write", "--kind", "backlog", "--file", str(source), "--version", "2"]) == 1
    assert "holds style-version 3, newer than the 2 offered" in capsys.readouterr().err
    assert main(["template", "--json", "--kind", "backlog"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["style_source"], shown["style"]) == ("default", place.read(".vault/styles/backlog.md"))
    assert main(["template", "--kind", "backlog"]) == 0
    assert "Style for backlog notes, from the default" in capsys.readouterr().out
    assert main(["template", "--json", "--kind", "task"]) == 0
    assert json.loads(capsys.readouterr().out)["style"] is None


def test_the_cli_never_writes_the_override(place: Place, tmp_path: Path, capsys):  # noqa: F811
    place.write(".vault/styles/backlog.override.md", OVERRIDE)
    source = tmp_path / "backlog-style.md"
    source.write_text("Default.\n", encoding="utf-8")
    assert main(["style", "write", "--kind", "backlog.override", "--file", str(source)]) == 1
    assert "is not a name of letters" in capsys.readouterr().err
    assert main(["style", "write", "--kind", "backlog", "--file", str(source)]) == 0
    assert "applies in place of it" in json.loads(capsys.readouterr().out)["note"]
    assert place.read(".vault/styles/backlog.override.md") == OVERRIDE
    assert main(["template", "--json", "--kind", "backlog"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["style"], shown["style_source"]) == (OVERRIDE, "override")


def test_the_cli_reads_a_style_with_its_etag_locally_and_on_a_server(place: Place, capsys):  # noqa: F811
    place.write(".vault/styles/backlog.override.md", OVERRIDE)
    assert main(["read", "--json", ".vault/styles/backlog.override.md"]) == 0
    read = json.loads(capsys.readouterr().out)
    assert read["text"] == OVERRIDE and read["etag"]


def test_the_cli_takes_only_a_style_version_of_one_or_more(place: Place, tmp_path: Path, capsys):  # noqa: F811
    source = tmp_path / "backlog-style.md"
    source.write_text("One line.\n", encoding="utf-8")
    for bad in ("-1", "0", "two"):
        with pytest.raises(SystemExit) as exited:
            main(["style", "write", "--kind", "backlog", "--file", str(source), "--version", bad])
        assert exited.value.code == 2
        assert "an integer of 1 or more" in capsys.readouterr().err
    assert main(["read", "--json", ".vault/styles/backlog.md"]) == 1, "nothing was written"
