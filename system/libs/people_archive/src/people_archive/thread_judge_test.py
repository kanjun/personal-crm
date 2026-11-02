import pytest

from people_archive.data_types import Direction
from people_archive.identity import resolve_and_store_people
from people_archive.primitives import HandleKey
from people_archive.store import (
    ArchiveStore,
    BeeperChatRow,
    BeeperMessageRow,
    BeeperParticipantRow,
)
from people_archive.testing import WORK_ACCOUNT, make_parsed_email, utc
from people_archive.thread_judge import (
    ThreadJudgeError,
    _parse,
    _prompt_for,
    _sample,
    _thread_for,
    unnamed_handles,
)


def test_sample_keeps_the_opening_and_closing_of_a_long_thread() -> None:
    messages = [(i, 0, str(i)) for i in range(40)]
    sampled = _sample(messages)
    assert len(sampled) == 20 and sampled[0][0] == 0 and sampled[11][0] == 11 and sampled[12][0] == 32 and sampled[-1][0] == 39


def test_parse_reads_kinds_and_rejects_short_answers() -> None:
    lines = _parse('[{"n": 1, "kind": "warm", "name": "Vincent", "who": "ex-colleague", "confidence": 0.9}]', 1)
    assert lines[0].kind == "warm" and lines[0].name == "Vincent"
    with pytest.raises(ThreadJudgeError, match="Answered 1 of 2"):
        _parse('[{"n": 1, "kind": "warm"}]', 2)


def test_unnamed_numbers_get_their_thread_and_group_context(archive_store: ArchiveStore) -> None:
    with archive_store.transaction():
        archive_store.upsert_message(make_parsed_email("e1", "ada@example.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 2), "hi", "hello", False, "Mira Vale"))
        archive_store.upsert_beeper_chat(
            BeeperChatRow(chat_id="d1", account_id="im", network="iMessage", chat_type="single", title="+1 555-000-0001", participants_json="[]", last_activity=utc(2026, 3, 4)),
            [
                BeeperParticipantRow(chat_id="d1", participant_id="me", handle_key=HandleKey("phone:+10000000000"), full_name="the user", phone="+10000000000", username="", is_self=True),
                BeeperParticipantRow(chat_id="d1", participant_id="p1", handle_key=HandleKey("phone:+15550000001"), full_name="", phone="+15550000001", username="", is_self=False),
            ],
        )
        archive_store.upsert_beeper_chat(
            BeeperChatRow(chat_id="g1", account_id="wa", network="WhatsApp", chat_type="group", title="Offsite 2026", participants_json="[]", last_activity=utc(2026, 3, 5)),
            [
                BeeperParticipantRow(chat_id="g1", participant_id="me", handle_key=HandleKey("phone:+10000000000"), full_name="the user", phone="+10000000000", username="", is_self=True),
                BeeperParticipantRow(chat_id="g1", participant_id="p1", handle_key=HandleKey("phone:+15550000001"), full_name="", phone="+15550000001", username="", is_self=False),
            ],
        )
        archive_store.upsert_beeper_messages([
            BeeperMessageRow(message_id="m1", chat_id="d1", timestamp=utc(2026, 3, 1), sender_id="me", sender_name="the user", is_sender=True, text="Hi, it's the user"),
            BeeperMessageRow(message_id="m2", chat_id="d1", timestamp=utc(2026, 3, 2), sender_id="p1", sender_name="", is_sender=False, text="this is vincent!"),
        ])
    resolve_and_store_people(archive_store, [WORK_ACCOUNT])

    handles = unnamed_handles(archive_store)
    assert handles == [HandleKey("phone:+15550000001")]  # Ada has a name; the number does not
    thread = _thread_for(archive_store, handles[0])
    assert thread is not None
    prompt = _prompt_for([thread])
    assert "2026-03-01 the user: Hi, it\'s the user" in prompt or "the user: Hi, it's the user" in prompt
    assert "Them: this is vincent!" in prompt and 'WhatsApp group "Offsite 2026"' in prompt
