from __future__ import annotations

import subprocess
from pathlib import Path

GIT_TIMEOUT = 10


def git(args: list[str], cwd: str | Path) -> str:
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def repo_root(cwd: str | Path) -> Path | None:
    top = git(["rev-parse", "--show-toplevel"], cwd)
    return Path(top) if top else None


def main_checkout(cwd: str | Path) -> Path | None:
    """The checkout every worktree of this repo shares: its own toplevel outside a worktree."""
    common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    if not common:
        return None
    common_dir = Path(common)
    if common_dir.name == ".git":
        return common_dir.parent
    if git(["rev-parse", "--is-bare-repository"], common_dir) == "true":
        return common_dir
    # A submodule or a separate git dir: the common dir is not next to a checkout.
    return repo_root(cwd)


def repo_name(cwd: str | Path) -> str:
    checkout = main_checkout(cwd) or Path(cwd)
    return checkout.name.removesuffix(".git")


def in_main_checkout(cwd: str | Path) -> Path:
    """Where cwd sits in the main checkout, so note paths written there match a worktree."""
    here = Path(cwd).resolve()
    root, main = repo_root(cwd), main_checkout(cwd)
    if root is None or main is None or root == main or not here.is_relative_to(root):
        return here
    return main / here.relative_to(root)


def current_branch(cwd: str | Path) -> str | None:
    return git(["branch", "--show-current"], cwd) or None


def is_checkout(path: Path) -> bool:
    return (path / ".git").exists()
