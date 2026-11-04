from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_cli_hooks import make_repo

from mindin.cli import main
from mindin.config import adopt_plugin_config, plugin_options
from mindin.frontmatter import parse

STORAGE_VARS = ("VAULT_PATH", "S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET", "S3_PREFIX", "S3_REGION")


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("VAULT_SCHEMA", raising=False)
    for name in STORAGE_VARS:
        monkeypatch.setenv(name, "")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_" + name, "")
    return claude_dir


@pytest.fixture
def configured(vault_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    return vault_dir


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    main_checkout = make_repo(tmp_path / "repos", "orchard")
    path = tmp_path / "repos" / "wt1.orchard"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "task-1", str(path)], cwd=main_checkout, check=True)
    return path


def run(capsys: pytest.CaptureFixture, *argv: str) -> dict:
    assert main(list(argv)) == 0, capsys.readouterr().err
    return json.loads(capsys.readouterr().out)


def set_schema(vault_dir: Path, text: str) -> None:
    schema = vault_dir / ".vault" / "schema.yml"
    schema.write_text(schema.read_text(encoding="utf-8") + text, encoding="utf-8")


def note(vault_dir: Path, key: str) -> tuple[dict, str]:
    return parse((vault_dir / key).read_text(encoding="utf-8"))


def test_task_open_names_the_main_checkout_from_a_worktree(configured: Path, worktree: Path, capsys):
    opened = run(capsys, "task", "open", "--name", "Prune the grove", "--cwd", str(worktree))
    assert opened == {
        "path": "Projects/orchard-prune-the-grove.md",
        "created": True,
        "repo": "orchard",
        "area": "orchard",
    }
    frontmatter, body = note(configured, opened["path"])
    assert frontmatter["title"] == "Prune the grove"
    assert frontmatter["status"] == "active" and frontmatter["kind"] == "task"
    assert frontmatter["area"] == "[[orchard]]"
    assert frontmatter["tags"] == ["orchard"]
    assert frontmatter["path"].endswith("repos/orchard")
    assert frontmatter["source"] == "branch task-1"
    assert body.split() == [
        "##",
        "Problem",
        "##",
        "Lösning",
        "##",
        "Beslut",
        "##",
        "Testbeslut",
        "##",
        "Utanför",
        "ramen",
    ]


def test_task_open_is_in_the_index_after_the_write(configured: Path, worktree: Path, capsys):
    opened = run(capsys, "task", "open", "--name", "Prune", "--cwd", str(worktree))
    found = run_search(capsys)
    assert opened["path"] in found


def run_search(capsys: pytest.CaptureFixture) -> str:
    assert main(["search", "prune"]) == 0
    return capsys.readouterr().out


def test_task_open_twice_gives_two_stems(configured: Path, worktree: Path, capsys):
    first = run(capsys, "task", "open", "--name", "Prune", "--cwd", str(worktree))
    second = run(capsys, "task", "open", "--name", "Prune", "--cwd", str(worktree))
    assert first["path"] == "Projects/orchard-prune.md"
    assert second["path"] == "Projects/orchard-prune-2.md"


def test_task_open_avoids_a_stem_taken_in_the_archive(configured: Path, worktree: Path, capsys):
    (configured / "Archive" / "orchard-prune.md").write_text((configured / "Archive" / "old-plan.md").read_text())
    assert (
        run(capsys, "task", "open", "--name", "Prune", "--cwd", str(worktree))["path"] == "Projects/orchard-prune-2.md"
    )


def test_task_open_falls_back_to_the_repo_name_without_an_area(configured: Path, tmp_path: Path, capsys):
    repo = make_repo(tmp_path / "repos", "quince")
    opened = run(capsys, "task", "open", "--name", "First", "--cwd", str(repo))
    assert opened["area"] is None and opened["repo"] == "quince"
    assert note(configured, opened["path"])[0]["area"] == "[[quince]]"


def test_task_open_with_note_claims_a_backlog_note(configured: Path, worktree: Path, capsys):
    run(capsys, "task", "backlog", "Projects/orchard-draft.md", "--priority", "low")
    assert note(configured, "Projects/orchard-draft.md")[0]["status"] == "backlog"
    claimed = run(capsys, "task", "open", "--note", "Projects/orchard-draft.md", "--cwd", str(worktree))
    assert claimed == {"path": "Projects/orchard-draft.md", "created": False, "repo": "orchard", "area": "orchard"}
    assert note(configured, "Projects/orchard-draft.md")[0]["status"] == "active"


def test_task_open_fails_on_one_line_for_a_missing_note(configured: Path, worktree: Path, capsys):
    assert main(["task", "open", "--note", "Projects/nope.md", "--cwd", str(worktree)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "mindin: no such note: Projects/nope.md"


def test_task_check_reports_empty_sections_in_a_swedish_vault(configured: Path, worktree: Path, capsys):
    path = run(capsys, "task", "open", "--name", "Prune", "--cwd", str(worktree))["path"]
    target = configured / path
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("## Problem\n", "## Problem\nGrenarna växer in i staketet.\n"), encoding="utf-8")
    checked = run(capsys, "task", "check", path, "--need", "problem,solution,decisions")
    assert checked == {"missing": ["solution", "decisions"]}


def test_task_check_reads_the_english_headings(configured: Path, worktree: Path, capsys):
    set_schema(configured, "language: en\nother_languages: [sv]\n")
    path = run(capsys, "task", "open", "--name", "Prune", "--cwd", str(worktree))["path"]
    target = configured / path
    text = target.read_text(encoding="utf-8")
    assert "## Solution" in text and "## Out of scope" in text and "Lösning" not in text
    filled = text.replace("## Solution\n", "## Solution\n\n- cut the low branches\n")
    target.write_text(filled.replace("## Decisions\n", "## Decisions\n   \n"), encoding="utf-8")
    assert run(capsys, "task", "check", path, "--need", "problem,solution,decisions") == {
        "missing": ["problem", "decisions"]
    }


def test_task_check_takes_headings_the_vault_overrides(configured: Path, worktree: Path, capsys):
    set_schema(configured, "task:\n  sections:\n    sv: { problem: Varför }\n")
    path = run(capsys, "task", "open", "--name", "Prune", "--cwd", str(worktree))["path"]
    assert "## Varför" in (configured / path).read_text(encoding="utf-8")
    assert run(capsys, "task", "check", path, "--need", "problem") == {"missing": ["problem"]}


def test_task_check_counts_subheadings_but_not_a_heading_in_a_code_fence(configured: Path, capsys):
    body = "## Problem\n### Detalj\n\n## Lösning\n```\n## Beslut\n```\n## Beslut\n\n"
    frontmatter = "---\ntitle: x\ndate: 2026-01-01\nupdated: 2026-01-01\ntags: [orchard]\nstatus: active\n"
    (configured / "Projects" / "orchard-x.md").write_text(frontmatter + 'area: "[[orchard]]"\n---\n\n' + body)
    assert run(capsys, "task", "check", "Projects/orchard-x.md") == {"missing": ["decisions"]}


def test_task_close_moves_the_note_to_the_archive(configured: Path, worktree: Path, capsys):
    path = run(capsys, "task", "open", "--name", "Prune", "--cwd", str(worktree))["path"]
    closed = run(capsys, "task", "close", path)
    assert closed == {"path": "Archive/orchard-prune.md"}
    assert not (configured / path).exists()
    assert note(configured, closed["path"])[0]["status"] == "complete"


def test_task_backlog_parks_with_a_medium_priority(configured: Path, capsys):
    assert run(capsys, "task", "backlog", "Projects/orchard-fresh.md") == {"path": "Projects/orchard-fresh.md"}
    frontmatter, _ = note(configured, "Projects/orchard-fresh.md")
    assert frontmatter["status"] == "backlog" and frontmatter["priority"] == "medium"


def test_log_append_writes_the_main_checkouts_log(configured: Path, worktree: Path, capsys):
    argv = ["log", "append", "--cwd", str(worktree), "--line", "Pruned", "--commit", "abc1234", "--commit", "def5678"]
    assert run(capsys, *argv) == {"path": "Log/orchard-log.md"}
    last = (configured / "Log" / "orchard-log.md").read_text(encoding="utf-8").rstrip().splitlines()[-1]
    assert "Pruned | branch: task-1 | commits: abc1234 def5678 | [[orchard]]" in last


def test_log_file_name_comes_from_the_schema(configured: Path, worktree: Path, capsys):
    set_schema(configured, 'log:\n  file_format: "{repo}-logg.md"\n')
    assert run(capsys, "log", "path", "--repo", "orchard") == {"path": "Log/orchard-logg.md"}
    appended = run(capsys, "log", "append", "--cwd", str(worktree), "--line", "Pruned", "--branch", "main")
    assert appended == {"path": "Log/orchard-logg.md"}
    assert (configured / "Log" / "orchard-logg.md").is_file()


def test_status_without_config_is_unconfigured_and_exits_zero(capsys):
    status = run(capsys, "status")
    assert status["configured"] is False and status["writable"] is None and status["backend"] is None
    assert status["version"]
    assert run(capsys, "status", "--probe")["writable"] is False


def test_status_reports_a_writable_local_vault(configured: Path, capsys):
    status = run(capsys, "status", "--probe")
    assert status["configured"] is True and status["backend"] == "local" and status["writable"] is True
    assert not (configured / ".vault" / "write-probe").exists()


def test_status_without_probe_writes_nothing(configured: Path, capsys, monkeypatch: pytest.MonkeyPatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("status wrote to the vault")

    monkeypatch.setattr("mindin.backends.local.LocalBackend.put", refuse)
    monkeypatch.setattr("mindin.backends.local.LocalBackend.delete", refuse)
    status = run(capsys, "status")
    assert status["configured"] is True and status["backend"] == "local" and status["writable"] is None


def write_settings(claude_dir: Path, options: dict, plugin: str = "vault@gronare") -> None:
    settings = {"pluginConfigs": {"other@x": {"options": {"vault_path": "/wrong"}}, plugin: {"options": options}}}
    (claude_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def test_plugin_options_map_to_the_storage_variables(isolated: Path):
    write_settings(
        isolated, {"s3_endpoint": "https://s3.test", "s3_bucket": "b", "s3_secret_key": "", "unrelated": "x"}
    )
    assert plugin_options() == {"S3_ENDPOINT": "https://s3.test", "S3_BUCKET": "b"}


def test_commands_fall_back_to_the_plugin_config(isolated: Path, vault_dir: Path, worktree: Path, capsys):
    write_settings(isolated, {"vault_path": str(vault_dir)})
    assert run(capsys, "status")["backend"] == "local"
    assert run(capsys, "task", "open", "--name", "Prune", "--cwd", str(worktree))["path"] == "Projects/orchard-prune.md"
    assert (vault_dir / "Projects" / "orchard-prune.md").is_file()


def test_every_command_finds_the_server_in_the_plugin_config(isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    write_settings(isolated, {"vault_server_url": "https://vault.example.test", "s3_bucket": "old"})
    asked = []
    monkeypatch.setattr("mindin.remote.tool_text", lambda url, tool, **arguments: asked.append((url, tool)) or "")
    for command in (["search", "orchard"], ["backlog"], ["lint"]):
        assert main(command) == 0, command
    assert [tool for _, tool in asked] == ["search", "backlog", "lint"]
    assert {url for url, _ in asked} == {"https://vault.example.test"}


def test_the_environment_wins_over_the_plugin_config(isolated: Path, configured: Path, tmp_path: Path):
    write_settings(isolated, {"vault_path": str(tmp_path / "elsewhere")})
    assert adopt_plugin_config() == "env"
    assert os.environ["VAULT_PATH"] == str(configured)


def test_sensitive_options_come_from_claude_codes_credentials(isolated: Path):
    write_settings(isolated, {"s3_endpoint": "https://s3.test", "s3_bucket": "b", "s3_access_key": "ak"})
    secrets = {"pluginSecrets": {"vault@gronare": {"s3_secret_key": "sk"}}}
    (isolated / ".credentials.json").write_text(json.dumps(secrets), encoding="utf-8")
    assert plugin_options() == {
        "S3_ENDPOINT": "https://s3.test",
        "S3_BUCKET": "b",
        "S3_ACCESS_KEY": "ak",
        "S3_SECRET_KEY": "sk",
    }


def test_a_secret_in_settings_wins_over_the_credentials(isolated: Path):
    write_settings(
        isolated, {"s3_endpoint": "e", "s3_bucket": "b", "s3_access_key": "ak", "s3_secret_key": "from-settings"}
    )
    (isolated / ".credentials.json").write_text(
        json.dumps({"pluginSecrets": {"vault@gronare": {"s3_secret_key": "x"}}}), encoding="utf-8"
    )
    assert plugin_options()["S3_SECRET_KEY"] == "from-settings"
