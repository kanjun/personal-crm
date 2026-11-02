from pathlib import Path

import pytest

from people_archive.data_types import Direction
from people_archive.errors import StoreError
from people_archive.identity import resolve_and_store_people
from people_archive.primitives import GmailMessageId, PersonId, SourceKind
from people_archive.store import ArchiveStore, open_archive_store, parse_tag_list
from people_archive.testing import (
    WORK_ACCOUNT,
    make_contact_card,
    make_parsed_email,
    utc,
)


def test_messages_round_trip_and_participants_are_indexed(archive_store: ArchiveStore) -> None:
    message = make_parsed_email("m1", "ada@example.com", ("owner@example.com", "bob@example.com"), Direction.RECEIVED, utc(2026, 1, 5), "Quarterly plan", "Let us meet about the plan", False, "Mira Vale")
    with archive_store.transaction():
        archive_store.upsert_message(message)

    stored = archive_store.get_message(WORK_ACCOUNT, GmailMessageId("m1"))
    participants = archive_store.execute_read("SELECT role, address FROM message_participants WHERE message_id = ? ORDER BY role, address", ["m1"])

    assert stored == message
    assert participants == [("from", "ada@example.com"), ("to", "bob@example.com"), ("to", "owner@example.com")]


def test_full_text_search_finds_body_words_and_highlights(archive_store: ArchiveStore) -> None:
    with archive_store.transaction():
        archive_store.upsert_message(make_parsed_email("m1", "ada@example.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 5), "Plan", "Let us meet about the engine", False, "Ada"))
        archive_store.upsert_message(make_parsed_email("m2", "bob@example.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 6), "Lunch", "Sandwiches?", False, "Bob"))

    hits = archive_store.search_messages("engine", 10)

    assert [str(h.message_id) for h in hits] == ["m1"]
    assert "[engine]" in hits[0].snippet


def test_raw_records_are_compressed_round_tripped_and_dedupable(archive_store: ArchiveStore) -> None:
    raw = {"id": "x1", "payload": {"headers": [{"name": "Subject", "value": "hi"}]}}
    with archive_store.transaction():
        archive_store.upsert_raw_record(SourceKind.GMAIL, WORK_ACCOUNT, "x1", raw, utc(2026, 1, 1))

    assert archive_store.get_raw_record(SourceKind.GMAIL, WORK_ACCOUNT, "x1") == raw
    assert archive_store.existing_record_ids(SourceKind.GMAIL, WORK_ACCOUNT, {"x1", "x2"}) == {"x1"}


def test_status_reports_counts_and_bounds(archive_store: ArchiveStore) -> None:
    with archive_store.transaction():
        archive_store.upsert_message(make_parsed_email("m1", "ada@example.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 5), "a", "b", False, "Ada"))
        archive_store.upsert_message(make_parsed_email("m2", "ada@example.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 3, 5), "c", "d", False, "Ada"))

    status = archive_store.status()

    assert status.message_count_by_account == {str(WORK_ACCOUNT): 2}
    assert status.oldest_message_at == utc(2026, 1, 5)
    assert status.newest_message_at == utc(2026, 3, 5)


def test_person_edits_overlay_the_persons_row_and_survive_a_rebuild(archive_store: ArchiveStore) -> None:
    with archive_store.transaction():
        archive_store.upsert_contact(make_contact_card("people/1", "Mira Vale", ("ada@example.com",), (), "Example Workshop"))
    resolve_and_store_people(archive_store, [WORK_ACCOUNT])
    person_id = PersonId(archive_store.execute_read("SELECT person_id FROM persons WHERE canonical_name = 'Mira Vale'", [])[0][0])

    archive_store.set_person_edit(person_id, "organization", "Babbage & Co")
    archive_store.set_person_edit(person_id, "notes", "  Met at the Royal Society.  ")
    archive_store.set_person_edit(person_id, "interests", "Poetry, poetry, #Mathematics")
    resolve_and_store_people(archive_store, [WORK_ACCOUNT])

    assert archive_store.execute_read("SELECT organization FROM persons WHERE person_id = ?", [person_id]) == [("Babbage & Co",)]
    assert archive_store.person_edits(person_id) == {"organization": "Babbage & Co", "notes": "Met at the Royal Society.", "interests": "poetry, mathematics"}

    archive_store.set_person_edit(person_id, "organization", "")

    assert archive_store.execute_read("SELECT organization FROM persons WHERE person_id = ?", [person_id]) == [("Example Workshop",)]
    assert archive_store.person_edits(person_id) == {"notes": "Met at the Royal Society.", "interests": "poetry, mathematics"}
    with pytest.raises(StoreError):
        archive_store.set_person_edit(person_id, "score", "9")


def test_parse_tag_list_normalizes_what_was_typed() -> None:
    assert parse_tag_list("Meditation, #solarpunk\nmeditation, , AI Agents") == ("meditation", "solarpunk", "ai agents")



def test_open_honors_the_requested_lock_wait(tmp_path) -> None:
    with open_archive_store(tmp_path / "a.sqlite", lock_wait_seconds=7) as store:
        assert store.execute_read("PRAGMA busy_timeout", []) == [(7000,)]


def test_people_list_is_served_from_a_covering_index(tmp_path: Path) -> None:
    with open_archive_store(tmp_path / "archive.sqlite") as store:
        plan = store.execute_read(
            "EXPLAIN QUERY PLAN SELECT person_id FROM persons WHERE is_human = 1 AND is_colleague = 0 "
            "ORDER BY score DESC, last_contact_ts DESC LIMIT 60", [],
        )
        counts = store.execute_read("EXPLAIN QUERY PLAN SELECT is_colleague, tier, COUNT(*) FROM persons WHERE is_human=1 GROUP BY is_colleague, tier", [])

    assert any("persons_list_score" in str(step) for step in plan)
    assert not any("TEMP B-TREE FOR ORDER BY" in str(step) for step in plan)
    assert any("COVERING INDEX" in str(step) for step in counts)


def test_user_exclusion_survives_identity_refresh(archive_store: ArchiveStore) -> None:
    from people_archive.store import JudgmentRow

    with archive_store.transaction():
        archive_store.upsert_message(make_parsed_email("excluded1", "ada@example.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 5), "Hello", "Hello", False, "Ada"))
    resolve_and_store_people(archive_store, (WORK_ACCOUNT,))
    pid = PersonId(str(archive_store.execute_read("SELECT person_id FROM persons WHERE canonical_name = 'Ada'", [])[0][0]))
    archive_store.save_judgments([JudgmentRow(person_id=pid, verdict="EXCLUDED", reason="User removed from contacts", judged_at=utc(2026, 1, 6), model="user")])
    resolve_and_store_people(archive_store, (WORK_ACCOUNT,))
    assert archive_store.execute_read("SELECT is_human FROM persons WHERE person_id = ?", [str(pid)]) == [(0,)]
    assert archive_store.get_message(WORK_ACCOUNT, GmailMessageId("excluded1")) is not None
