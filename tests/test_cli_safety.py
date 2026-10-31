from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from test_cli_hooks import make_repo
from test_cli_tasks import isolated, write_settings  # noqa: F401

from notes_vault_mcp import hooks
from notes_vault_mcp.backends import NotFound
from notes_vault_mcp.cli import main, one_line
from notes_vault_mcp.config import claude_credentials, plugin_options, read_json
from notes_vault_mcp.hooks import read_hook_input

SECRET = "sekrit-s3-key-never-printed"


@pytest.fixture
def claude_dir(isolated: Path) -> Path:  # noqa: F811
    return isolated


def feed(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


# --- hook input -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", '"text"', "null", "{"])
def test_hook_input_that_is_not_an_object_is_an_empty_dict(raw: str):
    assert read_hook_input(raw) == {}


def test_session_start_with_malformed_stdin_still_exits_zero(vault, monkeypatch, capsys):
    feed(monkeypatch, "{not json")
    assert main(["hook", "session-start"]) == 0
    assert capsys.readouterr().out


def test_stop_with_a_cwd_that_does_not_exist_is_silent(vault, monkeypatch, capsys):
    feed(monkeypatch, json.dumps({"cwd": "/nonexistent/dir/for/vault/tests"}))
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out == ""


def test_stop_with_a_transcript_path_that_is_a_directory_treats_nothing_as_touched(tmp_path: Path):
    assert hooks.touched_notes(str(tmp_path)) == set()
    assert hooks.touched_notes("/nonexistent/transcript.jsonl") == set()


def test_a_transcript_with_broken_lines_is_read_past_them(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    lines = [
        "{broken tool_use vault",
        json.dumps({"type": "tool_use", "name": "mcp__vault__close", "input": {"path": "Projects/x.md"}}),
        json.dumps(
            {"type": "tool_use", "name": "mcp__vault__move_file", "input": {"source": 5, "dest": "Projects/y.md"}}
        ),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert hooks.touched_notes(str(transcript)) == {"Projects/x.md", "Projects/y.md"}


def test_a_write_through_the_claude_ai_connector_counts_as_touched(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    line = json.dumps(
        {"type": "tool_use", "name": "mcp__claude_ai_Vault__write_file", "input": {"path": "Projects/z.md"}}
    )
    transcript.write_text(line + "\n", encoding="utf-8")
    assert hooks.touched_notes(str(transcript)) == {"Projects/z.md"}


def test_a_hostile_note_date_reaches_git_as_one_argument(tmp_path: Path):
    repo = make_repo(tmp_path / "repos", "orchard")
    marker = tmp_path / "pwned"
    since = f"2026-01-01; touch {marker}"
    assert isinstance(hooks._commits_since(repo, since, ["app.py"]), str)
    assert not marker.exists()


# --- the json commands ----------------------------------------------------------------------------------------


def test_a_failing_backend_error_is_one_line_without_the_secret(claude_dir: Path, monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setenv("S3_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("S3_SECRET_KEY", SECRET)
    monkeypatch.setenv("S3_BUCKET", "b")
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("AWS_RETRY_MODE", "standard")
    repo = make_repo(tmp_path / "repos", "orchard")
    assert main(["task", "open", "--name", "x", "--cwd", str(repo)]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.count("\n") == 1 and err.startswith("notes-vault-mcp: ")
    assert SECRET not in err and "ak" not in err.split("notes-vault-mcp: ", 1)[1].split()


def test_one_line_prefixes_and_flattens_any_exception():
    assert one_line(NotFound("Projects/x.md")) == "notes-vault-mcp: no such note: Projects/x.md"
    assert one_line(RuntimeError("two\nlines   here")) == "notes-vault-mcp: two lines here"
    assert one_line(RuntimeError("notes-vault-mcp: already prefixed")) == "notes-vault-mcp: already prefixed"


def test_changelog_with_a_bad_period_fails_on_one_line(vault, capsys):
    try:
        code = main(["changelog", "orchard", "yesterday"])
    except Exception:
        code = -1
    assert code == 2
    assert capsys.readouterr().err.count("\n") == 1


def test_status_never_touches_the_keychain_when_a_config_dir_is_set(claude_dir: Path, monkeypatch, capsys):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the keychain was read")

    monkeypatch.setattr("notes_vault_mcp.config.subprocess.run", refuse)
    write_settings(claude_dir, {"s3_endpoint": "https://s3.test", "s3_bucket": "b"})
    assert main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["configured"] is False


def test_credentials_that_are_not_json_read_as_empty(claude_dir: Path):
    (claude_dir / ".credentials.json").write_text("{nope", encoding="utf-8")
    assert claude_credentials() == {}
    (claude_dir / ".credentials.json").write_text("[1]", encoding="utf-8")
    assert claude_credentials() == {}


def test_a_secret_from_the_credentials_never_reaches_status_output(claude_dir: Path, capsys):
    write_settings(claude_dir, {"s3_endpoint": "https://s3.test", "s3_bucket": "b", "s3_access_key": "ak"})
    secrets = {
        "pluginSecrets": {"vault@gronare": {"s3_secret_key": SECRET}},
        "claudeAiOauth": {"accessToken": "oauth-t"},
    }
    (claude_dir / ".credentials.json").write_text(json.dumps(secrets), encoding="utf-8")
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert SECRET not in out and "oauth-t" not in out
    assert json.loads(out)["backend"] == "s3"


@pytest.mark.parametrize(
    "settings",
    [
        {"pluginConfigs": []},
        {"pluginConfigs": {"vault@x": "text"}},
        {"pluginConfigs": {"vault@x": {"options": "text"}}},
        {"pluginConfigs": {"vault@x": {"options": {"vault_path": 5, "s3_bucket": ""}}}},
        {"pluginConfigs": {"vaulty@x": {"options": {"vault_path": "/elsewhere"}}}},
    ],
)
def test_plugin_options_ignore_malformed_or_foreign_config(claude_dir: Path, settings: dict):
    (claude_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    assert plugin_options() == {}


def test_read_json_tolerates_garbage():
    assert read_json("") == {} and read_json("[]") == {} and read_json('{"a": 1}') == {"a": 1}


def test_the_status_command_reports_an_s3_backend_without_opening_it(claude_dir: Path, monkeypatch, capsys):
    monkeypatch.setenv("S3_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("S3_SECRET_KEY", SECRET)
    monkeypatch.setenv("S3_BUCKET", "b")
    monkeypatch.setattr(
        "notes_vault_mcp.cli.open_vault", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened"))
    )
    assert main(["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status == {"configured": True, "version": status["version"], "backend": "s3", "writable": None}


def test_git_helpers_survive_a_cwd_that_is_not_a_directory(tmp_path: Path):
    from notes_vault_mcp.git import current_branch, main_checkout, repo_name, repo_root

    missing = tmp_path / "missing"
    assert repo_root(missing) is None and main_checkout(missing) is None and current_branch(missing) is None
    assert repo_name(missing) == "missing"


def test_a_worktree_name_never_becomes_a_repo_name(tmp_path: Path):
    repo = make_repo(tmp_path / "repos", "orchard")
    worktree = tmp_path / "repos" / "wt.orchard"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "t", str(worktree)], cwd=repo, check=True)
    from notes_vault_mcp.git import repo_name

    assert repo_name(worktree) == "orchard"
