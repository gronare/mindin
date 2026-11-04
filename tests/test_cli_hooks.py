from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from mindin import git, hooks, notes
from mindin.cli import main
from mindin.config import env
from mindin.vault import Vault

BACKEND_VARS = ("VAULT_PATH", "S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET")


def make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "First commit"],
        cwd=repo,
        check=True,
    )
    return repo


def head_sha(repo: Path) -> str:
    args = ["git", "rev-parse", "--short=7", "HEAD"]
    return subprocess.run(args, cwd=repo, capture_output=True, text=True).stdout.strip()


def feed(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def test_env_resolves_the_claude_plugin_option_prefix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_VAULT_TOKEN", "from-plugin")
    assert env("VAULT_TOKEN") == "from-plugin"


def test_env_prefers_the_bare_variable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_TOKEN", "direct")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_VAULT_TOKEN", "from-plugin")
    assert env("VAULT_TOKEN") == "direct"


def test_session_start_prints_the_context(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "orchard")
    feed(monkeypatch, {"cwd": str(repo), "session_id": "abc"})
    assert main(["hook", "session-start"]) == 0
    out = capsys.readouterr().out
    assert "Areas/orchard.md" in out
    assert "Projects/orchard-fresh.md" in out


def test_session_start_ends_with_how_to_read_the_vault(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "orchard")
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "session-start"]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith("not a path.")
    assert "`read_file(path)`" in out and "never look for the vault's files in the filesystem" in out


def test_session_start_exits_zero_with_no_backend(monkeypatch: pytest.MonkeyPatch, capsys):
    for name in BACKEND_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_" + name, raising=False)
    feed(monkeypatch, {"cwd": "/tmp"})
    assert main(["hook", "session-start"]) == 0
    assert capsys.readouterr().out.startswith("vault: mindin: no vault configured")


def test_session_start_lists_commits_newer_than_the_note(
    vault: Vault, vault_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    note = vault_dir / "Areas" / "frame.md"
    note.write_text(
        "---\ntitle: frame\ndate: 2020-01-01\nupdated: 2020-01-01\n"
        f"tags: [frame]\nstatus: active\nkind: system\npath: {repo}\n---\n\nKärnan.\n",
        encoding="utf-8",
    )
    vault.index.sync(force=True)
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "session-start"]) == 0
    out = capsys.readouterr().out
    assert "commits since the note — Areas/frame.md" in out
    assert "First commit" in out


def test_stop_blocks_when_a_commit_is_not_logged(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "frame")
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert head_sha(repo) in payload["reason"]


def commit_in(repo: Path, message: str) -> str:
    identity = ["-c", "user.email=t@example.com", "-c", "user.name=T"]
    subprocess.run(["git", *identity, "commit", "-q", "--allow-empty", "-m", message], cwd=repo, check=True)
    return head_sha(repo)


def fast_forward_from_a_teammate(repo: Path, tmp_path: Path) -> str:
    teammate = tmp_path / "teammate"
    subprocess.run(["git", "clone", "-q", str(repo), str(teammate)], check=True)
    sha = commit_in(teammate, "Teammate's commit")
    subprocess.run(["git", "pull", "-q", "--ff-only", str(teammate), "HEAD"], cwd=repo, check=True)
    return sha


def test_stop_ignores_commits_that_arrived_by_fast_forward(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    notes.log_append(vault, "frame", "Första bygget", commits=(head_sha(repo),))
    fast_forward_from_a_teammate(repo, tmp_path)
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out == ""


def test_stop_names_only_the_sessions_own_commit_after_a_fast_forward(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    notes.log_append(vault, "frame", "Första bygget", commits=(head_sha(repo),))
    theirs = fast_forward_from_a_teammate(repo, tmp_path)
    mine = commit_in(repo, "My commit")
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    reason = json.loads(capsys.readouterr().out)["reason"]
    assert mine in reason
    assert theirs not in reason


def test_stop_counts_cherry_picks_and_merge_commits_but_not_the_merged_branch(tmp_path):
    repo = make_repo(tmp_path / "repos", "frame")
    teammate = tmp_path / "teammate"
    subprocess.run(["git", "clone", "-q", str(repo), str(teammate)], check=True)
    to_pick = commit_in(teammate, "To pick")
    theirs = commit_in(teammate, "Teammate's commit")
    mine = commit_in(repo, "My commit")
    identity = ["-c", "user.email=t@example.com", "-c", "user.name=T"]
    subprocess.run(["git", "fetch", "-q", str(teammate), "HEAD"], cwd=repo, check=True)
    cherry_pick = ["git", *identity, "cherry-pick", "--allow-empty", to_pick]
    subprocess.run(cherry_pick, cwd=repo, check=True, capture_output=True)
    picked = head_sha(repo)
    subprocess.run(["git", *identity, "merge", "-q", "FETCH_HEAD", "-m", "Merge theirs"], cwd=repo, check=True)
    merge = head_sha(repo)
    shas = [sha for sha, _ in hooks._recent_commits(repo)]
    assert {mine, picked, merge} <= set(shas)
    assert to_pick not in shas
    assert theirs not in shas


def test_stop_counts_commits_rebased_by_pull_but_not_the_upstream(tmp_path):
    repo = make_repo(tmp_path / "repos", "frame")
    teammate = tmp_path / "teammate"
    subprocess.run(["git", "clone", "-q", str(repo), str(teammate)], check=True)
    theirs = commit_in(teammate, "Teammate's commit")
    commit_in(repo, "My commit")
    identity = ["-c", "user.email=t@example.com", "-c", "user.name=T"]
    subprocess.run(["git", *identity, "pull", "-q", "--rebase", str(teammate), "HEAD"], cwd=repo, check=True)
    shas = [sha for sha, _ in hooks._recent_commits(repo)]
    assert head_sha(repo) in shas
    assert theirs not in shas


def test_stop_falls_back_to_recent_commits_without_a_reflog(tmp_path):
    repo = make_repo(tmp_path / "repos", "frame")
    (repo / ".git" / "logs" / "HEAD").unlink()
    assert [sha for sha, _ in hooks._recent_commits(repo)] == [head_sha(repo)]


def test_stop_is_silent_once_the_commit_is_logged(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "frame")
    notes.log_append(vault, "frame", "Första bygget", commits=(head_sha(repo),))
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out == ""


def test_stop_is_silent_when_the_hook_is_already_active(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    feed(monkeypatch, {"cwd": str(repo), "stop_hook_active": True})
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out == ""


def test_stop_is_silent_when_switched_off(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "frame")
    monkeypatch.setenv("VAULT_STOP_HOOK", "off")
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out == ""


def write_transcript(path: Path, tool: str, arguments: dict) -> Path:
    tool_use = {"type": "tool_use", "name": f"mcp__plugin_vault_vault__{tool}", "input": arguments}
    entry = {"type": "assistant", "message": {"role": "assistant", "content": [tool_use]}}
    path.write_text("not json\n" + json.dumps(entry) + "\n", encoding="utf-8")
    return path


def test_stop_blocks_on_a_stale_note_the_session_touched(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "workshop")
    notes.log_append(vault, "workshop", "Bygget", commits=(head_sha(repo),))
    transcript = write_transcript(tmp_path / "t.jsonl", "read_file", {"path": "Projects/workshop-stale.md"})
    feed(monkeypatch, {"cwd": str(repo), "transcript_path": str(transcript)})
    assert main(["hook", "stop"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert "Projects/workshop-stale.md" in payload["reason"]
    assert "systemMessage" not in payload


def test_stop_stays_silent_about_stale_notes_the_session_did_not_touch(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "workshop")
    notes.log_append(vault, "workshop", "Bygget", commits=(head_sha(repo),))
    transcript = write_transcript(tmp_path / "t.jsonl", "write_file", {"path": "Projects/other.md"})
    feed(monkeypatch, {"cwd": str(repo), "transcript_path": str(transcript)})
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_stop_treats_a_missing_transcript_as_nothing_touched(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "workshop")
    notes.log_append(vault, "workshop", "Bygget", commits=(head_sha(repo),))
    feed(monkeypatch, {"cwd": str(repo), "transcript_path": str(tmp_path / "missing.jsonl")})
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_stop_blocks_on_an_unlogged_commit_without_naming_untouched_notes(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "workshop")
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert head_sha(repo) in payload["reason"]
    assert "Projects/workshop-stale.md" not in payload["reason"]
    assert "systemMessage" not in payload


def test_stop_outside_a_git_repo_is_silent(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    loose = tmp_path / "loose"
    loose.mkdir()
    feed(monkeypatch, {"cwd": str(loose)})
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out == ""


def add_worktree(repo: Path, path: Path, branch: str) -> Path:
    subprocess.run(["git", "worktree", "add", "-q", "-b", branch, str(path)], cwd=repo, check=True)
    return path


def test_repo_name_is_the_main_checkout_in_a_worktree(tmp_path):
    repo = make_repo(tmp_path / "repos", "orchard")
    worktree = add_worktree(repo, tmp_path / "repos" / "wt1.orchard", "fix-booking")
    assert hooks.repo_name(worktree) == "orchard"
    assert git.main_checkout(worktree) == repo.resolve()
    assert git.current_branch(worktree) == "fix-booking"


def test_repo_name_of_a_bare_repo_drops_the_git_suffix(tmp_path):
    repo = make_repo(tmp_path / "repos", "orchard")
    bare = tmp_path / "bare" / "orchard.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True)
    worktree = tmp_path / "bare" / "wt1.orchard"
    subprocess.run(["git", "worktree", "add", "-q", str(worktree)], cwd=bare, check=True)
    assert hooks.repo_name(worktree) == "orchard"


def test_in_main_checkout_maps_a_worktree_subdirectory(tmp_path):
    repo = make_repo(tmp_path / "repos", "orchard")
    worktree = add_worktree(repo, tmp_path / "repos" / "wt1.orchard", "fix-booking")
    (worktree / "app").mkdir()
    assert git.in_main_checkout(worktree / "app") == repo.resolve() / "app"
    assert git.in_main_checkout(repo) == repo.resolve()


def test_session_start_in_a_worktree_loads_the_main_repo(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "orchard")
    worktree = add_worktree(repo, tmp_path / "repos" / "wt1.orchard", "fix-booking")
    feed(monkeypatch, {"cwd": str(worktree)})
    assert main(["hook", "session-start"]) == 0
    assert "Areas/orchard.md" in capsys.readouterr().out
    assert vault.index.known_repos() == [("orchard", str(repo.resolve()))]


def test_session_start_in_a_worktree_lists_commits_on_note_paths_in_the_main_checkout(
    vault: Vault, vault_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    (vault_dir / "Areas" / "frame-app.md").write_text(
        "---\ntitle: frame app\ndate: 2020-01-01\nupdated: 2020-01-01\n"
        f"tags: [frame]\nstatus: active\nkind: system\npath: {repo}/app.py\n---\n\nAppen.\n",
        encoding="utf-8",
    )
    vault.index.sync(force=True)
    worktree = add_worktree(repo, tmp_path / "repos" / "wt1.frame", "fix-app")
    feed(monkeypatch, {"cwd": str(worktree)})
    assert main(["hook", "session-start"]) == 0
    out = capsys.readouterr().out
    assert "commits since the note — Areas/frame-app.md" in out
    assert "First commit" in out


def test_stop_in_a_worktree_names_the_main_repo_and_the_branch(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    notes.log_append(vault, "frame", "Första bygget", commits=(head_sha(repo),))
    worktree = add_worktree(repo, tmp_path / "repos" / "wt1.frame", "fix-app")
    mine = commit_in(worktree, "Fix the app")
    feed(monkeypatch, {"cwd": str(worktree)})
    assert main(["hook", "stop"]) == 0
    reason = json.loads(capsys.readouterr().out)["reason"]
    assert f"log_append(repo='frame', branch='fix-app', line='<what landed>', commits=['{mine}'])" in reason
    assert "wt1.frame" not in reason


def test_stop_in_a_worktree_is_silent_once_logged_under_the_main_repo(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    worktree = add_worktree(repo, tmp_path / "repos" / "wt1.frame", "fix-app")
    mine = commit_in(worktree, "Fix the app")
    notes.log_append(vault, "frame", "Appen lagad", commits=(mine,), branch="fix-app")
    feed(monkeypatch, {"cwd": str(worktree)})
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out == ""


def test_init_writes_the_schema_and_the_bases(vault: Vault, vault_dir: Path, capsys):
    assert main(["init"]) == 0
    assert (vault_dir / ".vault" / "schema.yml").exists()
    for name in ("Areas.base", "Open tasks.base", "Resources.base"):
        assert (vault_dir / name).exists()
    assert "## Vault" in capsys.readouterr().out


def test_init_keeps_an_existing_file_without_force(vault: Vault, vault_dir: Path):
    marker = "synonyms:\n  - [widget, komponent]\n"
    (vault_dir / ".vault" / "schema.yml").write_text(marker, encoding="utf-8")
    main(["init"])
    assert (vault_dir / ".vault" / "schema.yml").read_text(encoding="utf-8") == marker


def test_init_force_overwrites(vault: Vault, vault_dir: Path):
    (vault_dir / ".vault" / "schema.yml").write_text("synonyms: []\n", encoding="utf-8")
    main(["init", "--force"])
    assert "stale_after_days" in (vault_dir / ".vault" / "schema.yml").read_text(encoding="utf-8")


def test_search_command_prints_rows(vault: Vault, capsys):
    assert main(["search", "orchard", "--limit", "2"]) == 0
    assert capsys.readouterr().out.startswith("2 of 5")


def test_sync_command_reports_the_count(vault: Vault, capsys):
    assert main(["sync", "--rebuild"]) == 0
    assert "indexed 13 notes of 13" in capsys.readouterr().out


def test_lint_command_can_write_a_note(vault: Vault, vault_dir: Path, capsys):
    assert main(["lint", "--write", "Log/lint.md"]) == 0
    assert (vault_dir / "Log" / "lint.md").read_text(encoding="utf-8").startswith("---\ntitle: Lint ")


def test_changelog_prints_log_commits_and_notes(vault: Vault, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "orchard")
    assert main(["changelog", "orchard", "2026-08", "--repo-path", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "# orchard — 2026-08" in out
    assert "Release-grinden mäter A/B" in out
    assert "## commits" in out
    assert "[[orchard-fresh]]" in out


def test_serve_rejects_http_without_a_public_url(vault: Vault, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.delenv("VAULT_PUBLIC_URL", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_VAULT_PUBLIC_URL", raising=False)
    assert main(["serve", "--transport", "http"]) == 2
    assert "VAULT_PUBLIC_URL" in capsys.readouterr().err


def test_repo_name_falls_back_to_the_directory(tmp_path):
    loose = tmp_path / "not-a-repo"
    loose.mkdir()
    assert hooks.repo_name(loose) == "not-a-repo"


def test_session_start_follows_a_symlinked_repo_path(
    vault: Vault, vault_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "repos")
    note = vault_dir / "Areas" / "frame.md"
    note.write_text(
        "---\ntitle: frame\ndate: 2020-01-01\nupdated: 2020-01-01\n"
        f"tags: [frame]\nstatus: active\nkind: system\npath: {link / 'frame'}\n---\n\nKärnan.\n",
        encoding="utf-8",
    )
    vault.index.sync(force=True)
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "session-start"]) == 0
    assert "commits since the note — Areas/frame.md" in capsys.readouterr().out


def test_commits_since_shows_the_count_and_the_newest_ten(tmp_path):
    import subprocess

    from mindin.hooks import _commits_since

    root = tmp_path / "repo"
    root.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
    }
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    for i in range(12):
        (root / "f.txt").write_text(str(i))
        subprocess.run(["git", "add", "f.txt"], cwd=root, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", f"commit {i}"], cwd=root, check=True, env=env)
    text = _commits_since(root, "2000-01-01", [str(root)])
    assert text.startswith("12 commits, newest 10:")
    assert len(text.splitlines()) == 11


def vault_call(tool: str, server: str = "plugin_vault_vault") -> dict:
    tool_use = {"type": "tool_use", "name": f"mcp__{server}__{tool}", "input": {"path": "Projects/orchard-fresh.md"}}
    return {"type": "assistant", "uuid": f"a-{tool}", "message": {"role": "assistant", "content": [tool_use]}}


def prompt(uuid: str, text: str = "Låt oss beskära äppelträden i mars", **extra) -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "origin": {"kind": "human"},
        "message": {"role": "user", "content": text},
        **extra,
    }


def not_prompts() -> list[dict]:
    result = {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
    return [
        {"type": "user", "uuid": "n1", "message": {"role": "user", "content": [result]}},
        {"type": "user", "uuid": "n2", "isMeta": True, "message": {"role": "user", "content": "Caveat: the bench"}},
        {"type": "user", "uuid": "n3", "isCompactSummary": True, "message": {"role": "user", "content": "Summary"}},
        {"type": "user", "uuid": "n4", "message": {"role": "user", "content": "<bash-input>ls</bash-input>"}},
        {"type": "user", "uuid": "n5", "message": {"role": "user", "content": "<command-name>/clear</command-name>"}},
        {"type": "user", "uuid": "n6", "origin": {"kind": "task-notification"}, "message": {"content": "done"}},
        {"type": "user", "uuid": "n7", "origin": {"kind": "peer"}, "message": {"content": "Report from the shed"}},
        {
            "type": "user",
            "uuid": "n8",
            "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]},
        },
    ]


def transcript_of(path: Path, entries: list[dict]) -> Path:
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return path


def test_only_the_users_own_prompts_count_as_prompts():
    assert [entry["uuid"] for entry in not_prompts() if hooks.is_prompt(entry)] == []
    images = [{"type": "image", "source": {}}, {"type": "text", "text": "Se verkstadens ritning"}]
    assert hooks.is_prompt({"type": "user", "uuid": "p", "message": {"content": images}})
    assert hooks.is_prompt({"type": "user", "uuid": "p", "message": {"content": "Plantera päron"}})


def test_vault_writes_are_told_apart_from_reads_and_other_servers():
    assert hooks.is_vault_write("mcp__plugin_vault_vault__write_file")
    assert hooks.is_vault_write("mcp__claude_ai_Vault__append_file")
    assert not hooks.is_vault_write("mcp__plugin_vault_vault__search")
    assert not hooks.is_vault_write("mcp__orchard__write_file")
    assert not hooks.is_vault_write("vault_write_file")


def test_prompts_are_counted_from_the_last_vault_write(tmp_path: Path):
    entries = [prompt("p1"), vault_call("search"), vault_call("backlog_add"), *not_prompts(), prompt("p2")]
    entries += [vault_call("read_file"), prompt("p3")]
    found = hooks.read_transcript(str(transcript_of(tmp_path / "t.jsonl", entries)))
    assert found.prompts_since_write == ["p2", "p3"]
    assert hooks.unsaved_prompts(found, "p2") == ["p3"]
    assert hooks.unsaved_prompts(found, "p3") == []
    assert hooks.unsaved_prompts(found, "p1") == ["p2", "p3"]


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path / "cache"


def stop_payload(monkeypatch: pytest.MonkeyPatch, capsys, payload: dict) -> dict | None:
    feed(monkeypatch, payload)
    assert main(["hook", "stop"]) == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def prompts(*uuids: str) -> list[dict]:
    return [prompt(uuid) for uuid in uuids]


def test_stop_waits_for_three_prompts_after_the_last_vault_write_and_asks_once(
    cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    transcript = transcript_of(tmp_path / "t.jsonl", [prompt("p0"), vault_call("write_file"), *prompts("p1", "p2")])
    payload = {"cwd": str(tmp_path), "session_id": "s-orchard", "transcript_path": str(transcript)}
    assert stop_payload(monkeypatch, capsys, payload) is None
    transcript_of(transcript, [prompt("p0"), vault_call("write_file"), *prompts("p1", "p2", "p3")])
    blocked = stop_payload(monkeypatch, capsys, payload)
    assert blocked["decision"] == "block"
    assert "the user wrote 3 messages since your last vault write" in blocked["reason"]
    assert stop_payload(monkeypatch, capsys, payload) is None
    transcript_of(transcript, [vault_call("write_file"), *prompts("p1", "p2", "p3", "p4", "p5")])
    assert stop_payload(monkeypatch, capsys, payload) is None
    transcript_of(transcript, [vault_call("write_file"), *prompts("p1", "p2", "p3", "p4", "p5", "p6")])
    assert "wrote 3 messages since" in stop_payload(monkeypatch, capsys, payload)["reason"]


@pytest.mark.parametrize(("setting", "threshold"), [("1", 1), ("5", 5), ("0", 3), ("-2", 3), ("many", 3), ("", 3)])
def test_the_prompt_threshold_comes_from_the_setting(monkeypatch: pytest.MonkeyPatch, setting: str, threshold: int):
    monkeypatch.setenv("VAULT_STOP_HOOK_PROMPTS", setting)
    assert hooks.prompt_threshold() == threshold


def test_stop_asks_at_a_custom_number_of_prompts(cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    transcript = transcript_of(tmp_path / "t.jsonl", [prompt("p1")])
    payload = {"cwd": str(tmp_path), "session_id": "s-orchard", "transcript_path": str(transcript)}
    monkeypatch.setenv("VAULT_STOP_HOOK_PROMPTS", "1")
    assert "wrote 1 message since" in stop_payload(monkeypatch, capsys, payload)["reason"]
    assert stop_payload(monkeypatch, capsys, {**payload, "session_id": "s-workshop"})["decision"] == "block"


def test_stop_is_silent_when_the_last_prompts_came_before_a_vault_write(
    cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    entries = [*prompts("p1", "p2", "p3"), vault_call("log_append", "claude_ai_Vault")]
    transcript = transcript_of(tmp_path / "t.jsonl", entries)
    payload = {"cwd": str(tmp_path), "session_id": "s-orchard", "transcript_path": str(transcript)}
    assert stop_payload(monkeypatch, capsys, payload) is None


def test_the_prompt_check_switches_off_alone(cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    transcript = transcript_of(tmp_path / "t.jsonl", prompts("p1", "p2", "p3"))
    payload = {"cwd": str(tmp_path), "session_id": "s-orchard", "transcript_path": str(transcript)}
    monkeypatch.setenv("VAULT_STOP_HOOK_PROMPTS", "off")
    assert stop_payload(monkeypatch, capsys, payload) is None
    monkeypatch.delenv("VAULT_STOP_HOOK_PROMPTS")
    monkeypatch.setenv("VAULT_STOP_HOOK", "off")
    assert stop_payload(monkeypatch, capsys, payload) is None
    assert stop_payload(monkeypatch, capsys, {**payload, "stop_hook_active": True}) is None


def test_stop_names_prompts_and_unlogged_commits_in_one_block(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    repo = make_repo(tmp_path / "repos", "workshop")
    transcript = transcript_of(tmp_path / "t.jsonl", prompts("p1", "p2", "p3"))
    payload = {"cwd": str(repo), "session_id": "s-workshop", "transcript_path": str(transcript)}
    blocked = stop_payload(monkeypatch, capsys, payload)
    assert head_sha(repo) in blocked["reason"]
    assert "the user wrote 3 messages since" in blocked["reason"]
    monkeypatch.setenv("VAULT_STOP_HOOK_PROMPTS", "off")
    assert "message" not in stop_payload(monkeypatch, capsys, payload)["reason"]


def test_the_commit_check_switches_off_alone(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    repo = make_repo(tmp_path / "repos", "orchard")
    transcript = transcript_of(tmp_path / "t.jsonl", prompts("p1", "p2"))
    payload = {"cwd": str(repo), "session_id": "s-orchard", "transcript_path": str(transcript)}
    monkeypatch.setenv("VAULT_STOP_HOOK_COMMITS", "off")
    assert stop_payload(monkeypatch, capsys, payload) is None
    transcript_of(transcript, prompts("p1", "p2", "p3"))
    blocked = stop_payload(monkeypatch, capsys, payload)
    assert "the user wrote 3 messages since" in blocked["reason"]
    assert head_sha(repo) not in blocked["reason"]
