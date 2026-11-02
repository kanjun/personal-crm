from datetime import datetime, timezone

from people_archive.calendar_pull import parse_calendar_event
from people_archive.testing import WORK_ACCOUNT


def test_parse_calendar_event_reads_attendees_series_and_times() -> None:
    raw = {
        "id": "evt1_20260105T170000Z", "recurringEventId": "evt1", "summary": "Weekly sync", "status": "confirmed",
        "start": {"dateTime": "2026-01-05T09:00:00-08:00"}, "end": {"dateTime": "2026-01-05T09:30:00-08:00"},
        "organizer": {"email": "owner@example.com", "self": True},
        "attendees": [
            {"email": "owner@example.com", "self": True, "responseStatus": "accepted"},
            {"email": "Ada@Example.com", "displayName": "Mira Vale", "responseStatus": "tentative"},
            {"email": "not an email"},
        ],
    }

    event = parse_calendar_event(WORK_ACCOUNT, raw)

    assert event is not None
    assert str(event.series_id) == "evt1"
    assert event.start == datetime(2026, 1, 5, 17, 0, tzinfo=timezone.utc)
    assert event.is_organized_by_owner is True
    assert [(str(a.address), a.name, a.is_self) for a in event.attendees] == [("owner@example.com", "", True), ("ada@example.com", "Mira Vale", False)]


def test_parse_calendar_event_skips_cancelled_stubs_and_reads_all_day_dates() -> None:
    assert parse_calendar_event(WORK_ACCOUNT, {"id": "x", "status": "cancelled"}) is None
    all_day = parse_calendar_event(WORK_ACCOUNT, {"id": "y", "start": {"date": "2026-02-01"}, "end": {"date": "2026-02-02"}})
    assert all_day is not None and all_day.start == datetime(2026, 2, 1, tzinfo=timezone.utc)
