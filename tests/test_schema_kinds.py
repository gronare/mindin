from __future__ import annotations

from pathlib import Path

import pytest

from mindin.backends.local import LocalBackend
from mindin.schema import Schema, deep_merge, default_schema_data, instructions, load_schema
from mindin.vault import Vault


def _default() -> dict:
    return default_schema_data()


def test_the_default_schema_gives_every_kind_a_rule():
    schema = Schema(_default())
    assert set(schema.kind_rules) == set(schema.kind_values)
    text = instructions(schema)
    assert "- kind is one of:\n  - system: one living note" in text
    assert "  - trap: a gotcha that bit once" in text


def test_a_vault_can_add_a_kind_with_its_rule_and_drop_one():
    merged = deep_merge(
        _default(), {"kinds": {"research": "reading notes on a topic; no task, no decision", "howto": None}}
    )
    schema = Schema(merged)
    assert "research" in schema.kind_values
    assert "howto" not in schema.kind_values
    text = instructions(schema)
    assert "  - research: reading notes on a topic" in text
    assert "howto" not in text


def test_a_plain_list_without_rules_still_renders_on_one_line():
    data = _default()
    del data["kinds"]
    schema = Schema(data)
    assert schema.kind_values == ["system", "task", "trap", "howto", "decision", "reference", "log"]
    assert "- kind is one of: system, task, trap, howto, decision, reference, log" in instructions(schema)


class Unreachable(LocalBackend):
    def get(self, key: str) -> tuple[str, str]:
        raise OSError("storage is down")


def test_a_vault_without_a_schema_file_gets_the_default_schema(tmp_path: Path):
    assert load_schema(LocalBackend(tmp_path)).data == default_schema_data()


def test_a_schema_that_cannot_be_read_is_an_error_not_the_default(tmp_path: Path):
    with pytest.raises(OSError, match="storage is down"):
        load_schema(Unreachable(tmp_path))


class Flaky(LocalBackend):
    down = True

    def get(self, key: str) -> tuple[str, str]:
        if self.down:
            raise OSError("storage is down")
        return super().get(key)


def test_a_schema_change_seen_while_storage_is_down_is_read_once_it_is_back(tmp_path: Path):
    backend = Flaky(tmp_path)
    vault = Vault(backend, Schema(default_schema_data()), index_path=tmp_path / "index.sqlite")
    (tmp_path / ".vault").mkdir()
    (tmp_path / ".vault" / "schema.yml").write_text("preset: simple\n", encoding="utf-8")
    vault.mark_schema_changed()
    with pytest.raises(OSError):
        vault.catch_up()
    backend.down = False
    vault.catch_up()
    assert vault.schema.preset == "simple"


def test_a_schema_change_marked_while_the_schema_is_being_read_is_read_next_time(tmp_path: Path):
    vault = Vault(LocalBackend(tmp_path), Schema(default_schema_data()), index_path=tmp_path / "index.sqlite")
    reload = vault.reload_schema
    seen: list[int] = []

    def reload_while_another_write_lands() -> None:
        reload()
        if not seen:
            vault.mark_schema_changed()
        seen.append(1)

    vault.reload_schema = reload_while_another_write_lands  # type: ignore[method-assign]
    vault.mark_schema_changed()
    vault.catch_up()
    vault.catch_up()
    assert len(seen) == 2


class NotText(LocalBackend):
    def get(self, key: str) -> tuple[str, str]:
        return b"\xff\xfe".decode("utf-8"), ""


def test_a_schema_that_is_not_text_is_a_schema_problem(tmp_path: Path):
    schema = load_schema(NotText(tmp_path))
    assert schema.problems == (".vault/schema.yml is not UTF-8 text",)
