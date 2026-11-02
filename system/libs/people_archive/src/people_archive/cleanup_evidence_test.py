import json
import sqlite3
import tracemalloc

from people_archive.calendar_pull import parse_calendar_event
from people_archive.cleanup_evidence import EvidenceArchive, PersonEvidenceProfile
from people_archive.data_types import Direction, MessageParticipant
from people_archive.primitives import EmailAddress, SourceKind
from people_archive.store import ArchiveStore
from people_archive.testing import WORK_ACCOUNT, make_parsed_email, utc


def _person() -> PersonEvidenceProfile:
    return PersonEvidenceProfile(id='sample', name='Alex', aliases=[], handles=['email:alex@example.com'],
                                 organization='', title='', score=0, has_user_edits=False)


def test_email_retains_sender_recipients_quotes_and_exact_original(archive_store: ArchiveStore) -> None:
    body = 'Hello!\nAlex Smith\nOn Tuesday Someone wrote:\n> Regards, Different Person'
    message = make_parsed_email('m1', 'alex@example.com', ('owner@example.com',), Direction.RECEIVED,
                                utc(2020, 1, 1), 'Hello', body, False, 'Alex')
    message = message.model_copy(update={'to_recipients': (MessageParticipant(name='Owner Person', address=EmailAddress('owner@example.com')), )})
    with archive_store.transaction():
        archive_store.upsert_message(message)
    archive = EvidenceArchive(archive_store.path)
    evidence = archive.evidence(_person())
    archive.close()
    email = next(item for item in evidence if item['kind'] == 'email')
    assert 'Alex Smith' in email['text']
    assert 'Different Person' in email['text']
    assert 'alex@example.com' in email['text']
    assert '["Owner Person", "owner@example.com"]' in email['text']
    assert json.loads(email['original_text'])['text'] == body
    assert 'recipients, not authors' in email['context']


def test_calendar_exact_name_binding_and_description(archive_store: ArchiveStore) -> None:
    raw = {'id': 'e1', 'summary': 'Alex / Owner', 'description': 'Alex Smith, Acme research',
           'start': {'dateTime': '2020-01-01T12:00:00Z'},
           'attendees': [{'email': 'alex@example.com', 'displayName': 'Alex Smith'},
                         {'email': 'other@example.com', 'displayName': 'Another Person'}]}
    event = parse_calendar_event(WORK_ACCOUNT, raw)
    assert event is not None
    with archive_store.transaction():
        archive_store.upsert_calendar_event(event)
        archive_store.upsert_raw_record(SourceKind.GOOGLE_CALENDAR, WORK_ACCOUNT, 'e1', raw, utc(2020, 1, 1))
    archive = EvidenceArchive(archive_store.path)
    evidence = archive.evidence(_person())
    archive.close()
    calendar = next(item for item in evidence if item['kind'] == 'calendar')
    assert '[["alex@example.com", "Alex Smith"' in calendar['text']
    assert 'Alex Smith, Acme research' in calendar['text']
    assert json.loads(calendar['original_text']) == raw


def test_sparse_archive_discloses_no_evidence(archive_store: ArchiveStore) -> None:
    archive = EvidenceArchive(archive_store.path)
    evidence = archive.evidence(_person())
    archive.close()
    assert len(evidence) == 1
    assert all(value == 0 for value in json.loads(evidence[0]['text'])['available_record_counts'].values())


def test_chat_only_attributes_matching_author(archive_store: ArchiveStore) -> None:
    with sqlite3.connect(archive_store.path) as connection:
        connection.executemany('INSERT INTO beeper_participants VALUES (?,?,?,?,?,?,?)',
            [('chat', 'alex-id', 'email:alex@example.com', 'Alex Smith', '', '', 0),
             ('chat', 'other-id', 'email:other@example.com', 'Other Person', '', '', 0)])
        connection.executemany('INSERT INTO beeper_messages VALUES (?,?,?,?,?,?,?)',
            [('m1', 'chat', 1, 'alex-id', 'Alex', 0, 'I am Alex Smith'),
             ('m2', 'chat', 2, 'other-id', 'Other', 0, 'I am someone else')])
    archive = EvidenceArchive(archive_store.path)
    evidence = archive.evidence(_person())
    archive.close()
    messages = [item for item in evidence if item['kind'] == 'chat_message']
    assert len(messages) == 1
    assert 'I am Alex Smith' in messages[0]['text']
    assert 'someone else' not in messages[0]['text']


def test_expansion_recovers_middle_of_long_email(archive_store: ArchiveStore) -> None:
    body = ('preface ' * 180) + '\nMy name is Alex Smith\n' + ('ending ' * 180)
    with archive_store.transaction():
        archive_store.upsert_message(make_parsed_email('long', 'alex@example.com', ('owner@example.com',), Direction.RECEIVED,
                                  utc(2020, 1, 1), 'Hello', body, False, 'Alex'))
    archive = EvidenceArchive(archive_store.path)
    initial = next(item for item in archive.evidence(_person()) if item['kind'] == 'email')
    expanded = next(item for item in archive.evidence(_person(), expanded=True) if item['kind'] == 'email')
    archive.close()
    assert initial['truncated']
    assert not expanded['truncated']
    assert 'My name is Alex Smith' in expanded['text']
    assert json.loads(initial['original_text'])['text'] == body


def test_evidence_memory_does_not_grow_with_full_message_history(archive_store: ArchiveStore) -> None:
    body = 'Long historical message. ' * 8000
    with archive_store.transaction():
        for number in range(60):
            archive_store.upsert_message(make_parsed_email(str(number), 'alex@example.com', ('owner@example.com',), Direction.RECEIVED,
                                        utc(2020, 1, 1), str(number), body, False, 'Alex'))
    with sqlite3.connect(archive_store.path) as connection:
        connection.executemany('INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?)',
            [('notes', str(number), 'Unrelated', 0, 1, body, '[]', '[]', '{}') for number in range(40)])
    tracemalloc.start()
    try:
        archive = EvidenceArchive(archive_store.path)
        evidence = archive.evidence(_person())
        archive.close()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 6_000_000
    coverage = json.loads(evidence[0]['text'])
    assert coverage['available_record_counts']['email'] == 60
    assert coverage['selected_record_counts']['email'] <= 4
    assert all(item['truncated'] for item in evidence if item['kind'] == 'email')
