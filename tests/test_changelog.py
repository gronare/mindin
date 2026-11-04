from __future__ import annotations

import json
import os
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest
from test_cli_hooks import feed, head_sha, make_repo

from mindin import changelog, hooks
from mindin.cli import main
from mindin.frontmatter import parse
from mindin.vault import Vault

THIS_MONTH = date.today().strftime("%Y-%m")


def commit(repo: Path, subject: str) -> str:
    (repo / "more.py").write_text(f"# {subject}\n", encoding="utf-8")
    subprocess.run(["git", "add", "more.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", subject], cwd=repo, check=True
    )
    return head_sha(repo)


def test_periods_due_adds_the_previous_month_in_the_first_week():
    assert changelog.periods_due(date(2026, 9, 3)) == ["2026-08", "2026-09"]
    assert changelog.periods_due(date(2026, 9, 20)) == ["2026-09"]
    assert changelog.periods_due(date(2027, 1, 2)) == ["2026-12", "2027-01"]


def test_period_key_follows_the_log_folder(vault: Vault):
    assert vault.schema.period_key("orchard", "2026-08") == "Log/orchard-2026-08.md"


def test_write_page_creates_the_month_page_with_the_log_area(vault: Vault, vault_dir: Path, tmp_path):
    repo = make_repo(tmp_path / "repos", "orchard")
    assert changelog.write_page(vault, "orchard", "2026-08", repo) is True
    frontmatter, body = parse((vault_dir / "Log/orchard-2026-08.md").read_text(encoding="utf-8"))
    assert frontmatter["area"] == "[[orchard]]"
    assert frontmatter["status"] == "complete"
    assert frontmatter["kind"] == "log"
    assert changelog.GENERATED_START in body and changelog.GENERATED_END in body
    assert "Release-grinden mäter A/B" in body
    assert "[[orchard-fresh]]" in body


def test_write_page_lists_only_the_repos_own_notes(vault: Vault, vault_dir: Path, tmp_path):
    repo = make_repo(tmp_path / "repos", "frame")
    changelog.write_page(vault, "frame", "2026-08", repo)
    body = (vault_dir / "Log/frame-2026-08.md").read_text(encoding="utf-8")
    assert "[[orchard-fresh]]" not in body


def test_write_page_keeps_prose_outside_the_markers_and_refreshes_the_block(vault: Vault, vault_dir: Path, tmp_path):
    repo = make_repo(tmp_path / "repos", "orchard")
    changelog.write_page(vault, "orchard", THIS_MONTH, repo)
    page = vault_dir / "Log/orchard-2026-08.md".replace("2026-08", THIS_MONTH)
    frontmatter, body = parse(page.read_text(encoding="utf-8"))
    assert frontmatter["status"] == "active"
    from mindin.frontmatter import dump

    page.write_text(dump(frontmatter, "## Sammanfattning\n\nEn månad av prosa.\n\n" + body), encoding="utf-8")
    vault.index.sync(force=True)
    sha = commit(repo, "Second commit")
    assert changelog.write_page(vault, "orchard", THIS_MONTH, repo) is True
    text = page.read_text(encoding="utf-8")
    assert "En månad av prosa." in text
    assert sha in text
    assert text.count(changelog.GENERATED_START) == 1


def test_write_page_is_a_no_op_when_nothing_changed(vault: Vault, vault_dir: Path, tmp_path):
    repo = make_repo(tmp_path / "repos", "orchard")
    changelog.write_page(vault, "orchard", "2026-08", repo)
    before = (vault_dir / "Log/orchard-2026-08.md").read_text(encoding="utf-8")
    assert changelog.write_page(vault, "orchard", "2026-08", repo) is False
    assert (vault_dir / "Log/orchard-2026-08.md").read_text(encoding="utf-8") == before


def test_write_all_covers_the_remembered_repos_and_skips_a_vanished_checkout(vault: Vault, vault_dir: Path, tmp_path):
    repo = make_repo(tmp_path / "repos", "orchard")
    vault.index.remember_repo("orchard", str(repo))
    vault.index.remember_repo("gone", str(tmp_path / "repos" / "gone"))
    written = changelog.write_all(vault, periods=["2026-08"])
    assert written == ["Log/orchard-2026-08.md"]
    assert not (vault_dir / "Log/gone-2026-08.md").exists()


def test_write_all_forgets_worktree_names_and_missing_roots(vault: Vault, vault_dir: Path, tmp_path):
    repo = make_repo(tmp_path / "repos", "orchard")
    worktree = tmp_path / "repos" / "wt1.orchard"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "fix-booking", str(worktree)], cwd=repo, check=True)
    vault.index.remember_repo("orchard", str(repo))
    vault.index.remember_repo("wt1.orchard", str(worktree))
    vault.index.remember_repo("gone", str(tmp_path / "repos" / "gone"))
    assert changelog.write_all(vault, periods=["2026-08"]) == ["Log/orchard-2026-08.md"]
    assert not (vault_dir / "Log/wt1.orchard-2026-08.md").exists()
    assert vault.index.known_repos() == [("orchard", str(repo))]


def test_session_start_remembers_the_repo(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "orchard")
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "session-start"]) == 0
    assert ("orchard", str(repo.resolve())) in vault.index.known_repos()


def test_stop_writes_the_month_pages_once_a_day(
    vault: Vault, vault_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "orchard")
    vault.index.remember_repo("orchard", str(repo))
    page = vault_dir / f"Log/orchard-{THIS_MONTH}.md"
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "block"
    assert page.exists()
    page.unlink()
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    assert not page.exists()
    vault.index.set_meta(hooks.CHANGELOG_META, (date.today() - timedelta(days=1)).isoformat())
    vault.index.db.commit()
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    assert page.exists()


def test_changelog_all_writes_the_pages_for_a_named_period(vault: Vault, vault_dir: Path, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "orchard")
    vault.index.remember_repo("orchard", str(repo))
    assert main(["changelog", "--all", "2026-08"]) == 0
    assert "wrote Log/orchard-2026-08.md" in capsys.readouterr().out
    assert (vault_dir / "Log/orchard-2026-08.md").exists()


def test_changelog_write_writes_one_page(vault: Vault, vault_dir: Path, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "orchard")
    assert main(["changelog", "orchard", "2026-08", "--repo-path", str(repo), "--write"]) == 0
    assert "wrote Log/orchard-2026-08.md" in capsys.readouterr().out
    assert main(["changelog", "orchard", "2026-08", "--repo-path", str(repo), "--write"]) == 0
    assert "unchanged Log/orchard-2026-08.md" in capsys.readouterr().out


def test_changelog_without_a_repo_or_all_fails_loudly(vault: Vault, capsys):
    assert main(["changelog"]) == 2


def commit_at(repo: Path, subject: str, moment: str) -> str:
    identity = ["-c", "user.email=t@example.com", "-c", "user.name=T"]
    dated = {**os.environ, "GIT_AUTHOR_DATE": moment, "GIT_COMMITTER_DATE": moment}
    subprocess.run(["git", *identity, "commit", "-q", "--allow-empty", "-m", subject], cwd=repo, check=True, env=dated)
    return head_sha(repo)


def test_a_period_takes_whole_days_at_both_ends(tmp_path):
    repo = make_repo(tmp_path / "repos", "orchard")
    first = commit_at(repo, "Just after midnight on the first", "2026-08-01T00:00:01")
    last = commit_at(repo, "Last minute of the month", "2026-08-31T23:59:59")
    after = commit_at(repo, "Just after midnight on the next first", "2026-09-01T00:00:01")
    august = [entry.split()[0] for entries in changelog.git_commits(repo, "2026-08").values() for entry in entries]
    assert first in august and last in august, "--since without a time takes the current time of day"
    assert after not in august, "--until without a time takes the current time of day"
