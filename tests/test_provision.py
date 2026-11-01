from __future__ import annotations

from pathlib import Path

from notes_vault_mcp import provision
from notes_vault_mcp.backends.local import LocalBackend


def test_initialize_writes_schema_bases_and_welcome(tmp_path: Path):
    backend = LocalBackend(tmp_path / "v")
    result = provision.initialize(backend, welcome={"DAV_URL": "https://x/dav/"})
    assert result.written == [
        ".vault/schema.yml",
        "Areas.base",
        "Open tasks.base",
        "Resources.base",
        "Backlog.base",
        "Log.base",
        "Welcome.md",
    ]
    assert result.kept == []
    welcome = backend.get("Welcome.md")[0]
    assert "https://x/dav/" in welcome and "{{" not in welcome
    assert backend.get(".vault/schema.yml")[0].startswith("version: 1\nlanguage: en")
    open_tasks = backend.get("Open tasks.base")[0]
    assert ".days > 30," in open_tasks and "{{" not in open_tasks


def test_initialize_keeps_what_exists_unless_forced(tmp_path: Path):
    backend = LocalBackend(tmp_path / "v")
    backend.put(".vault/schema.yml", "version: 1\n")
    result = provision.initialize(backend)
    assert result.kept == [".vault/schema.yml"]
    assert ".vault/schema.yml" not in result.written
    assert backend.get(".vault/schema.yml")[0] == "version: 1\n"
    forced = provision.initialize(backend, force=True)
    assert ".vault/schema.yml" in forced.written
    assert "language: en" in backend.get(".vault/schema.yml")[0]


def test_initialize_without_welcome_writes_no_welcome_note(tmp_path: Path):
    backend = LocalBackend(tmp_path / "v")
    result = provision.initialize(backend)
    assert "Welcome.md" not in result.written
    assert provision.is_empty(LocalBackend(tmp_path / "empty"))
    assert not provision.is_empty(backend)
