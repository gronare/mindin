from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from clock_support import environment

from mindin.index import Note
from mindin.search import age_days
from mindin.terminal import updated_words

STOCKHOLM = ZoneInfo("Europe/Stockholm")


def test_a_note_dated_five_days_back_is_five_days_old_just_after_midnight():
    today = date.today()
    just_after_midnight = datetime(today.year, today.month, today.day, 0, 5).timestamp()
    note = Note(key="Areas/orchard.md", updated=(today - timedelta(days=5)).isoformat())
    assert age_days(note, just_after_midnight) == 5


@pytest.mark.parametrize(
    ("updated", "now", "days"),
    [
        ("2026-09-14", datetime(2026, 9, 14, 23, 59, 30, tzinfo=UTC), 0),
        ("2026-09-14", datetime(2026, 9, 15, 0, 0, 30, tzinfo=UTC), 1),
        ("2026-09-30", datetime(2026, 10, 1, 0, 0, 30, tzinfo=UTC), 1),
        ("2026-12-31", datetime(2027, 1, 1, 0, 0, 30, tzinfo=UTC), 1),
        ("2028-02-28", datetime(2028, 2, 29, 12, 0, tzinfo=UTC), 1),
        ("2028-02-28", datetime(2028, 3, 1, 12, 0, tzinfo=UTC), 2),
        ("2027-02-28", datetime(2027, 3, 1, 12, 0, tzinfo=UTC), 1),
    ],
)
def test_a_notes_age_turns_over_at_midnight_and_across_months_years_and_leap_days(updated, now, days):
    with environment(TZ="UTC"):
        assert age_days(Note(key="Areas/orchard.md", updated=updated), now.timestamp()) == days


@pytest.mark.parametrize(
    ("updated", "now", "days"),
    [
        ("2027-03-27", datetime(2027, 3, 27, 23, 59, tzinfo=STOCKHOLM), 0),
        ("2027-03-27", datetime(2027, 3, 28, 0, 30, tzinfo=STOCKHOLM), 1),
        ("2027-03-27", datetime(2027, 3, 28, 3, 30, tzinfo=STOCKHOLM), 1),
        ("2026-10-24", datetime(2026, 10, 25, 23, 30, tzinfo=STOCKHOLM), 1),
        ("2026-10-24", datetime(2026, 10, 26, 0, 10, tzinfo=STOCKHOLM), 2),
    ],
)
def test_a_notes_age_counts_local_days_across_both_daylight_saving_changes(updated, now, days):
    with environment(TZ="Europe/Stockholm"):
        assert age_days(Note(key="Areas/orchard.md", updated=updated), now.timestamp()) == days


def test_a_note_updated_yesterday_reads_one_day_and_today_reads_today():
    assert updated_words(0) == "updated today"
    assert updated_words(1) == "updated 1 day ago"
    assert updated_words(59) == "updated 59 days ago"
    assert updated_words(60) == "updated 2 months ago"
