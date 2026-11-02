import csv
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

from people_archive.data_types import PullSummary
from people_archive.primitives import AccountEmail, SourceKind
from people_archive.store import ArchiveStore, MeetingRow

GRANOLA_ACCOUNT: Final[AccountEmail] = AccountEmail("granola@local")
# A Granola note is matched to the calendar event that starts within this window of its creation.
_MATCH_WINDOW: Final[timedelta] = timedelta(hours=2)
_CSV_COLUMNS: Final[tuple[str, ...]] = ("document_id", "document_title", "document_created", "summary", "notes", "transcript")


class GranolaRecord(FrozenModel):
    """One row of Granola's CSV export."""

    document_id: str = Field(description="Granola's id")
    title: str = Field(description="Meeting title")
    created: datetime = Field(description="When the note was created, UTC")
    summary: str = Field(description="Granola's summary")
    notes: str = Field(description="The user's own notes in Granola")
    transcript: str = Field(description="Full transcript")


@pure
def parse_granola_csv_text(rows: Sequence[dict[str, str]]) -> list[GranolaRecord]:
    records: list[GranolaRecord] = []
    for row in rows:
        created_raw = (row.get("document_created") or "").strip()
        if not created_raw or not row.get("document_id"):
            continue
        records.append(GranolaRecord(
            document_id=str(row["document_id"]), title=(row.get("document_title") or "").strip(),
            created=datetime.fromisoformat(created_raw.replace("Z", "+00:00")).astimezone(timezone.utc),
            summary=row.get("summary") or "", notes=row.get("notes") or "", transcript=row.get("transcript") or "",
        ))
    return records


def read_granola_csv(path: Path) -> list[GranolaRecord]:
    csv.field_size_limit(sys.maxsize)
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        return parse_granola_csv_text(list(csv.DictReader(handle)))


def _matching_event(store: ArchiveStore, record: GranolaRecord) -> tuple[str, str, list[str]] | None:
    """The calendar event closest in time to the note's creation, preferring a title match."""
    low = int((record.created - _MATCH_WINDOW).timestamp())
    high = int((record.created + _MATCH_WINDOW).timestamp())
    rows = store.execute_read(
        "SELECT event_id, summary, attendees_json, start_ts FROM calendar_events WHERE start_ts BETWEEN ? AND ?",
        [low, high],
    )
    if not rows:
        return None
    title = record.title.strip().lower()

    def rank(row: tuple[Any, ...]) -> tuple[int, int]:
        is_title_match = 0 if title and title == str(row[1]).strip().lower() else 1
        return (is_title_match, abs(int(row[3]) - int(record.created.timestamp())))

    best = min(rows, key=rank)
    attendees = [str(a[0]) for a in json.loads(best[2])]
    return str(best[0]), str(best[1]), attendees


def import_granola(store: ArchiveStore, csv_path: Path) -> PullSummary:
    records = read_granola_csv(csv_path)
    fetched_at = datetime.now(timezone.utc)
    matched = 0
    with store.transaction():
        for record in records:
            match = _matching_event(store, record)
            if match is not None:
                matched += 1
            store.upsert_raw_record(SourceKind.GRANOLA, GRANOLA_ACCOUNT, record.document_id, record.model_dump(mode="json"), fetched_at)
            store.upsert_meeting(MeetingRow(
                meeting_id=record.document_id, title=record.title, started=record.created, summary=record.summary,
                notes=record.notes, transcript=record.transcript,
                calendar_event_id=match[0] if match else "", attendee_emails=tuple(match[2]) if match else (),
            ))
    logger.info("Granola: {} meetings stored, {} matched to a calendar event", len(records), matched)
    return PullSummary(source=SourceKind.GRANOLA, account=GRANOLA_ACCOUNT, fetched_count=len(records), skipped_count=len(records) - matched, failed_count=0)
