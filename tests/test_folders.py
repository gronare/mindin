from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mindin import credentials
from mindin.config import cache_dir, config_dir


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv("VAULT_CACHE_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_VAULT_CACHE_DIR", raising=False)
    return tmp_path


def signed_in_under(folder: Path) -> None:
    folder.mkdir(parents=True)
    entry = {"key": "nvm_kept", "scope": "vault:read", "name": "laptop"}
    (folder / "credentials.json").write_text(json.dumps({"servers": {"https://vault.example.com": entry}}))


def test_the_former_config_folder_moves_and_keeps_the_machine_signed_in(home: Path):
    old = home / ".config" / "notes-vault-mcp"
    signed_in_under(old)
    assert config_dir() == home / ".config" / "mindin"
    assert (home / ".config" / "mindin" / "credentials.json").is_file()
    assert credentials.stored("https://vault.example.com")["key"] == "nvm_kept"
    assert old.is_symlink() and old.resolve() == (home / ".config" / "mindin").resolve()


def test_the_former_cache_moves_with_its_index(home: Path):
    old = home / ".cache" / "notes-vault-mcp"
    (old / "models").mkdir(parents=True)
    (old / "index-abc.sqlite").write_bytes(b"kept")
    assert cache_dir() == home / ".cache" / "mindin"
    assert (home / ".cache" / "mindin" / "index-abc.sqlite").read_bytes() == b"kept"
    assert (home / ".cache" / "mindin" / "models").is_dir()


def test_a_failed_move_reads_the_former_folder_where_it_is(home: Path, monkeypatch: pytest.MonkeyPatch):
    signed_in_under(home / ".config" / "notes-vault-mcp")

    def refuse(src: object, dst: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(os, "rename", refuse)
    assert config_dir() == home / ".config" / "notes-vault-mcp"
    assert credentials.stored("https://vault.example.com")["key"] == "nvm_kept"
    assert not (home / ".config" / "mindin").exists()


def test_nothing_moves_when_both_folders_exist(home: Path):
    signed_in_under(home / ".config" / "notes-vault-mcp")
    (home / ".config" / "mindin").mkdir()
    assert config_dir() == home / ".config" / "mindin"
    assert (home / ".config" / "notes-vault-mcp" / "credentials.json").is_file()
    assert not (home / ".config" / "notes-vault-mcp").is_symlink()
    assert credentials.stored("https://vault.example.com") is None


def test_a_machine_without_a_former_folder_gets_mindins_own(home: Path):
    assert config_dir() == home / ".config" / "mindin"
    assert cache_dir() == home / ".cache" / "mindin"
    assert not (home / ".config" / "notes-vault-mcp").exists()


def test_a_cache_named_in_the_environment_is_used_as_it_is(home: Path, monkeypatch: pytest.MonkeyPatch):
    (home / ".cache" / "notes-vault-mcp").mkdir(parents=True)
    monkeypatch.setenv("VAULT_CACHE_DIR", str(home / "elsewhere"))
    assert cache_dir() == home / "elsewhere"
    assert (home / ".cache" / "notes-vault-mcp").is_dir() and not (home / ".cache" / "mindin").exists()
