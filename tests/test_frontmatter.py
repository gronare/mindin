from __future__ import annotations

import datetime as dt

import pytest

from mindin.frontmatter import FrontmatterError, dump, parse, validate
from mindin.schema import Schema, load_schema

NOTE = """---
title: Test
date: 2026-01-02
updated: 2026-02-03
tags: [orchard, ci]
status: active
area: "[[orchard]]"
---

Body line.
"""


@pytest.fixture
def schema() -> Schema:
    return load_schema()


def test_parse_returns_mapping_and_body():
    frontmatter, body = parse(NOTE)
    assert frontmatter["title"] == "Test"
    assert frontmatter["date"] == dt.date(2026, 1, 2)
    assert frontmatter["tags"] == ["orchard", "ci"]
    assert body.strip() == "Body line."


def test_parse_without_frontmatter_returns_whole_text():
    assert parse("just text") == ({}, "just text")


def test_parse_raises_with_a_line_number_on_bad_yaml():
    with pytest.raises(FrontmatterError) as caught:
        parse("---\ntitle: ok\ntags: [unclosed\n---\nbody\n")
    assert caught.value.line >= 1


def test_dump_keeps_key_order_dates_and_inline_tags():
    frontmatter, body = parse(NOTE)
    text = dump(frontmatter, body)
    assert text.splitlines()[1:7] == [
        "title: Test",
        "date: 2026-01-02",
        "updated: 2026-02-03",
        "tags: [orchard, ci]",
        "status: active",
        'area: "[[orchard]]"',
    ]


def test_dump_round_trips_through_parse():
    frontmatter, body = parse(NOTE)
    again, again_body = parse(dump(frontmatter, body))
    assert again == frontmatter
    assert again_body.strip() == body.strip()


def test_dump_quotes_a_title_with_a_colon():
    text = dump({"title": "Orchard: plan"}, "body")
    assert 'title: "Orchard: plan"' in text


def test_validate_accepts_a_complete_note(schema: Schema):
    frontmatter, _ = parse(NOTE)
    assert validate(frontmatter, "Projects/test.md", schema) == []


def test_validate_reports_missing_required_fields(schema: Schema):
    problems = validate({"title": "x"}, "Resources/x.md", schema)
    assert "missing required field: date" in problems
    assert "missing required field: status" in problems


def test_validate_rejects_an_unknown_status(schema: Schema):
    frontmatter, _ = parse(NOTE)
    frontmatter["status"] = "halvklar"
    problems = validate(frontmatter, "Projects/test.md", schema)
    assert any("status 'halvklar'" in problem for problem in problems)


def test_validate_rejects_an_unknown_kind(schema: Schema):
    frontmatter, _ = parse(NOTE)
    frontmatter["kind"] = "pamflett"
    assert any("kind 'pamflett'" in problem for problem in validate(frontmatter, "Projects/t.md", schema))


def test_validate_requires_area_in_the_configured_folders(schema: Schema):
    frontmatter, _ = parse(NOTE)
    del frontmatter["area"]
    assert any("missing area" in problem for problem in validate(frontmatter, "Projects/t.md", schema))
    assert validate(frontmatter, "Areas/t.md", schema) == []


def test_validate_requires_area_to_be_a_wikilink(schema: Schema):
    frontmatter, _ = parse(NOTE)
    frontmatter["area"] = "orchard"
    assert any("must be a wikilink" in problem for problem in validate(frontmatter, "Projects/t.md", schema))


def test_validate_checks_the_tag_vocabulary_only_when_strict(schema: Schema):
    frontmatter, _ = parse(NOTE)
    frontmatter["tags"] = ["hittepå"]
    assert validate(frontmatter, "Projects/t.md", schema) == []
    strict = Schema({**schema.data, "tags": {"strict": True, "vocabulary": ["orchard"]}})
    assert any("hittepå" in problem for problem in validate(frontmatter, "Projects/t.md", strict))
