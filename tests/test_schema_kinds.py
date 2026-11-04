from __future__ import annotations

from mindin.schema import Schema, deep_merge, default_schema_data, instructions


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
