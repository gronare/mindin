from __future__ import annotations

import json
from pathlib import Path

import pytest

from mindin.search import render, search
from mindin.vault import Vault

QUERIES = json.loads((Path(__file__).parent / "fixtures" / "queries.json").read_text(encoding="utf-8"))
LIMIT = 15
MAX_RENDERED_CHARS = 6000


def test_the_fixture_carries_the_synthetic_queries():
    assert len(QUERIES) > 100
    assert all("query" in entry for entry in QUERIES)


@pytest.mark.parametrize("entry", QUERIES, ids=lambda entry: entry["query"][:40])
def test_every_query_stays_within_its_limit(vault: Vault, entry: dict):
    result = search(
        vault.index,
        vault.schema,
        entry["query"],
        limit=LIMIT,
        status=entry.get("status"),
        folder=entry.get("path"),
    )
    assert len(result.rows) <= LIMIT
    assert len(render(result)) < MAX_RENDERED_CHARS
