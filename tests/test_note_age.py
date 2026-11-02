from __future__ import annotations

from datetime import date, datetime, timedelta

from notes_vault_mcp.index import Note
from notes_vault_mcp.search import age_days


def test_a_note_dated_five_days_back_is_five_days_old_just_after_midnight():
    today = date.today()
    just_after_midnight = datetime(today.year, today.month, today.day, 0, 5).timestamp()
    note = Note(key="Areas/orchard.md", updated=(today - timedelta(days=5)).isoformat())
    assert age_days(note, just_after_midnight) == 5
