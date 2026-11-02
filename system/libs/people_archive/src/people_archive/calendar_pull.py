from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from imbue.imbue_common.pure import pure
from loguru import logger

from people_archive.data_types import CalendarEvent, EventAttendee, PullSummary
from people_archive.errors import InvalidArchiveValueError
from people_archive.interfaces import GoogleApiGatewayInterface
from people_archive.primitives import (
    AccountEmail,
    CalendarEventId,
    EmailAddress,
    SourceKind,
)
from people_archive.store import ArchiveStore

_CALENDAR_BASE: Final[str] = "https://www.googleapis.com/calendar/v3"
_PAGE_SIZE: Final[str] = "2500"
_SYNC_TOKEN_KEY: Final[str] = "sync_token"
# How far back the first pull reaches; the CRM's "who did I meet" signal is only useful
# over the same span as the email history.
_FIRST_PULL_FROM: Final[str] = "2019-01-01T00:00:00Z"
# Recurring series with no end date expand decades ahead; only the near future is of any use.
_LOOK_AHEAD_DAYS: Final[int] = 60


@pure
def _parse_event_time(value: Mapping[str, Any]) -> datetime:
    """An event's start or end: a dateTime with offset, or an all-day date (taken as midnight UTC)."""
    if "dateTime" in value:
        return datetime.fromisoformat(str(value["dateTime"]).replace("Z", "+00:00")).astimezone(timezone.utc)
    return datetime.fromisoformat(str(value["date"])).replace(tzinfo=timezone.utc)


@pure
def parse_calendar_event(account: AccountEmail, raw: Mapping[str, Any]) -> CalendarEvent | None:
    """Project one Calendar API event; None for cancelled stubs, which carry no times."""
    if raw.get("status") == "cancelled" or "start" not in raw:
        return None
    attendees: list[EventAttendee] = []
    for entry in raw.get("attendees") or []:
        try:
            address = EmailAddress(str(entry.get("email", "")))
        except InvalidArchiveValueError:
            continue
        attendees.append(EventAttendee(
            address=address,
            name=str(entry.get("displayName", "")),
            response_status=str(entry.get("responseStatus", "")),
            is_self=bool(entry.get("self", False)),
        ))
    organizer_raw = (raw.get("organizer") or {}).get("email", "")
    organizer: EmailAddress | None
    try:
        organizer = EmailAddress(str(organizer_raw)) if organizer_raw else None
    except InvalidArchiveValueError:
        organizer = None
    return CalendarEvent(
        account=account,
        event_id=CalendarEventId(str(raw["id"])),
        series_id=CalendarEventId(str(raw.get("recurringEventId") or raw["id"])),
        summary=str(raw.get("summary", "")),
        start=_parse_event_time(raw["start"]),
        end=_parse_event_time(raw.get("end") or raw["start"]),
        organizer=organizer,
        attendees=tuple(attendees),
        is_organized_by_owner=bool((raw.get("organizer") or {}).get("self", False)),
    )


def pull_calendar(gateway: GoogleApiGatewayInterface, store: ArchiveStore, account: AccountEmail) -> PullSummary:
    """Pull the primary calendar; the first run walks from 2019, later runs use the sync token."""
    sync_token = store.get_sync_value(SourceKind.GOOGLE_CALENDAR, account, _SYNC_TOKEN_KEY)
    fetched = 0
    page_token: str | None = ""
    fetched_at = datetime.now(timezone.utc)
    while page_token is not None:
        params: dict[str, str] = {"maxResults": _PAGE_SIZE, "singleEvents": "true"}
        if sync_token:
            params["syncToken"] = sync_token
        else:
            params["timeMin"] = _FIRST_PULL_FROM
            params["timeMax"] = (datetime.now(timezone.utc) + timedelta(days=_LOOK_AHEAD_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if page_token:
            params["pageToken"] = page_token
        page = gateway.get_json(account, f"{_CALENDAR_BASE}/calendars/primary/events", params)
        with store.transaction():
            for raw in page.get("items", []) or []:
                event = parse_calendar_event(account, raw)
                if event is None:
                    continue
                store.upsert_raw_record(SourceKind.GOOGLE_CALENDAR, account, str(raw["id"]), raw, fetched_at)
                store.upsert_calendar_event(event)
                fetched += 1
        page_token = page.get("nextPageToken") or None
        next_sync_token = page.get("nextSyncToken")
        if page_token is None and next_sync_token:
            with store.transaction():
                store.set_sync_value(SourceKind.GOOGLE_CALENDAR, account, _SYNC_TOKEN_KEY, str(next_sync_token))
    logger.info("Calendar {}: {} events stored", account, fetched)
    return PullSummary(source=SourceKind.GOOGLE_CALENDAR, account=account, fetched_count=fetched, skipped_count=0, failed_count=0)
