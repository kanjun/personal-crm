from datetime import datetime, timezone

from people_archive.data_types import Direction
from people_archive.identity import (
    _is_role_or_robot_address,
    _is_toll_free,
    find_merge_candidates,
    resolve_and_store_people,
    resolve_people,
)
from people_archive.primitives import EmailAddress, HandleKey, PhoneNumber
from people_archive.store import (
    ArchiveStore,
    BeeperChatRow,
    BeeperMessageRow,
    BeeperParticipantRow,
    ThreadJudgmentRow,
)
from people_archive.testing import (
    WORK_ACCOUNT,
    make_contact_card,
    make_parsed_email,
    utc,
)


def _seed_conversations(store: ArchiveStore) -> None:
    with store.transaction():
        store.upsert_message(make_parsed_email("a1", "ada@example.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 5), "hi", "hello there", False, "Mira Vale"))
        store.upsert_message(make_parsed_email("a2", "owner@example.com", ("ada@example.com",), Direction.SENT, utc(2026, 1, 6), "re: hi", "hi back", False, ""))
        store.upsert_message(make_parsed_email("a3", "ada@example.org", ("owner@example.com",), Direction.RECEIVED, utc(2026, 2, 1), "other address", "from my other mail", False, "Mira Vale"))
        store.upsert_message(make_parsed_email("r1", "no-reply@shop.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 7), "receipt", "thanks for buying", True, "Shop"))
        store.upsert_message(make_parsed_email("b1", "bob@example.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 3, 1), "intro", "nice to meet", False, "Bob Smith"))
        store.upsert_message(make_parsed_email("b2", "bob@other.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 3, 2), "again", "same bob?", False, "Bob Smith"))


def test_contact_card_merges_email_and_phone_into_one_person(archive_store: ArchiveStore) -> None:
    _seed_conversations(archive_store)
    with archive_store.transaction():
        archive_store.upsert_contact(make_contact_card("people/1", "Mira Vale", ("ada@example.com", "ada@example.org"), ("+1 (555) 010-0001",), "Example Workshop"))

    people = resolve_people(archive_store, [WORK_ACCOUNT])
    ada = next(p for p in people if "ada@example.com" in p.emails)

    assert set(ada.emails) == {"ada@example.com", "ada@example.org"}
    assert ada.phones == ("+15550100001",)
    assert ada.organization == "Example Workshop"
    assert ada.sent_count == 1 and ada.received_human_count == 2


def test_same_name_and_local_part_merges_but_different_local_part_only_proposes(archive_store: ArchiveStore) -> None:
    _seed_conversations(archive_store)

    people = resolve_people(archive_store, [WORK_ACCOUNT])
    ada_people = [p for p in people if any(e.startswith("ada@") for e in p.emails)]
    bob_people = [p for p in people if any(e.startswith("bob@") for e in p.emails)]

    # ada@example.com and ada@example.org share the name and the local part: one person.
    assert len(ada_people) == 1
    # bob@example.com and bob@other.com share the name and the local part too: one person.
    assert len(bob_people) == 1


def test_robot_senders_fail_the_human_gate(archive_store: ArchiveStore) -> None:
    _seed_conversations(archive_store)

    people = resolve_people(archive_store, [WORK_ACCOUNT])
    shop = next(p for p in people if "no-reply@shop.com" in p.emails)

    assert shop.is_human is False
    assert shop.received_automated_count == 1


def test_person_ids_stay_stable_across_runs(archive_store: ArchiveStore) -> None:
    _seed_conversations(archive_store)
    first = {p.emails[0]: p.person_id for p in resolve_and_store_people(archive_store, [WORK_ACCOUNT])}

    with archive_store.transaction():
        archive_store.upsert_message(make_parsed_email("a4", "ada@example.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 4, 1), "later", "one more", False, "Mira Vale"))
    second = {p.emails[0]: p.person_id for p in resolve_and_store_people(archive_store, [WORK_ACCOUNT])}

    assert second["ada@example.com"] == first["ada@example.com"]
    assert second["bob@example.com"] == first["bob@example.com"]


def test_merge_candidates_pair_people_with_the_same_full_name(archive_store: ArchiveStore) -> None:
    with archive_store.transaction():
        archive_store.upsert_message(make_parsed_email("c1", "alex.chen@one.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 1), "s", "t", False, "Alex Chen"))
        archive_store.upsert_message(make_parsed_email("c2", "achen@two.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 2), "s", "t", False, "Alex Chen"))

    people = resolve_people(archive_store, [WORK_ACCOUNT])
    candidates = find_merge_candidates(people)

    assert len(people) == 2
    assert len(candidates) == 1 and candidates[0][2] == "same full name"


def test_owners_other_addresses_and_spoofed_senders_are_not_people(archive_store: ArchiveStore, monkeypatch) -> None:
    monkeypatch.setenv("CRM_SELF_ADDRESSES", '["owner.smith@oldjob.com", "owner+list@example.com", "phisher@spoof.com"]')
    _seed_conversations(archive_store)
    with archive_store.transaction():
        archive_store.upsert_message(make_parsed_email("o1", "owner@example.com", ("ada@example.com", "owner.smith@oldjob.com", "owner+list@example.com"), Direction.SENT, utc(2026, 4, 1), "cc myself", "x", False, "Owner Smith"))
        archive_store.upsert_message(make_parsed_email("o2", "phisher@spoof.com", ("ada@example.com",), Direction.RECEIVED, utc(2026, 4, 2), "urgent", "wire money", False, "Owner Smith"))

    emails = {email for person in resolve_people(archive_store, [WORK_ACCOUNT]) for email in person.emails}

    assert "ada@example.com" in emails
    assert not {"owner.smith@oldjob.com", "owner+list@example.com", "phisher@spoof.com"} & emails


def test_group_chat_membership_is_not_contact(archive_store: ArchiveStore) -> None:
    """Only members who wrote get texting days; a silent unnamed number in a busy group is not listed as a person."""
    chat = BeeperChatRow(chat_id="g1", account_id="wa", network="WhatsApp", chat_type="group", title="Alumnae", participants_json="[]", last_activity=utc(2026, 3, 3))
    members = [
        BeeperParticipantRow(chat_id="g1", participant_id="me", handle_key=HandleKey("phone:+10000000000"), full_name="the user", phone="+10000000000", username="", is_self=True),
        BeeperParticipantRow(chat_id="g1", participant_id="p1", handle_key=HandleKey("phone:+15550000001"), full_name="Mira Vale", phone="+15550000001", username="", is_self=False),
        BeeperParticipantRow(chat_id="g1", participant_id="p2", handle_key=HandleKey("phone:+15550000002"), full_name="", phone="+15550000002", username="", is_self=False),
        BeeperParticipantRow(chat_id="g1", participant_id="p3", handle_key=HandleKey("phone:+15550000003"), full_name="", phone="+15550000003", username="", is_self=False),
    ]
    direct = BeeperChatRow(chat_id="d1", account_id="im", network="iMessage", chat_type="single", title="+1 555-000-0003", participants_json="[]", last_activity=utc(2026, 3, 4))
    direct_members = [
        BeeperParticipantRow(chat_id="d1", participant_id="me", handle_key=HandleKey("phone:+10000000000"), full_name="the user", phone="+10000000000", username="", is_self=True),
        BeeperParticipantRow(chat_id="d1", participant_id="p3", handle_key=HandleKey("phone:+15550000003"), full_name="", phone="+15550000003", username="", is_self=False),
    ]
    with archive_store.transaction():
        archive_store.upsert_beeper_chat(chat, members)
        archive_store.upsert_beeper_chat(direct, direct_members)
        archive_store.upsert_beeper_messages([
            BeeperMessageRow(message_id=f"m{day}", chat_id="g1", timestamp=utc(2026, 3, day), sender_id="p1", sender_name="Ada", is_sender=False, text="hi all")
            for day in range(1, 11)
        ] + [
            BeeperMessageRow(message_id="k1", chat_id="g1", timestamp=utc(2026, 3, 2), sender_id="me", sender_name="the user", is_sender=True, text="hello"),
            BeeperMessageRow(message_id="k2", chat_id="d1", timestamp=utc(2026, 3, 4), sender_id="me", sender_name="the user", is_sender=True, text="hey"),
        ])

    by_phone = {p.phones[0]: p for p in resolve_people(archive_store, [WORK_ACCOUNT]) if p.phones}

    ada = by_phone["+15550000001"]
    assert ada.is_human and ada.text_days_received == 10 and ada.text_days_sent == 0
    assert "+15550000002" not in by_phone  # never wrote, never texted directly: not a person in the archive
    texted_directly = by_phone["+15550000003"]
    assert texted_directly.is_human and texted_directly.text_days_sent == 1 and texted_directly.text_days_received == 0


def test_toll_free_numbers_and_relay_addresses_are_not_people(archive_store: ArchiveStore) -> None:
    direct = BeeperChatRow(chat_id="d1", account_id="im", network="iMessage", chat_type="single", title="+1 800-555-0100", participants_json="[]", last_activity=utc(2026, 3, 4))
    members = [
        BeeperParticipantRow(chat_id="d1", participant_id="me", handle_key=HandleKey("phone:+10000000000"), full_name="the user", phone="+10000000000", username="", is_self=True),
        BeeperParticipantRow(chat_id="d1", participant_id="p1", handle_key=HandleKey("phone:+18005550100"), full_name="", phone="+18005550100", username="", is_self=False),
    ]
    with archive_store.transaction():
        archive_store.upsert_beeper_chat(direct, members)
        archive_store.upsert_beeper_messages([BeeperMessageRow(message_id="m1", chat_id="d1", timestamp=utc(2026, 3, 4), sender_id="p1", sender_name="", is_sender=False, text="25% off sitewide")])
        archive_store.upsert_message(make_parsed_email("l1", "usr-abc123@user.luma-mail.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 1), "Re: dinner", "see you there", False, "Sam Guest"))
        archive_store.upsert_message(make_parsed_email("l2", "ada@example.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 2), "hi", "hello", False, "Mira Vale"))
        archive_store.upsert_contact(make_contact_card("c1", "Mira Vale", ("ada@example.com", "ada@user.luma-mail.com"), (), ""))

    people = {p.canonical_name: p for p in resolve_people(archive_store, [WORK_ACCOUNT])}

    assert not people["+18005550100"].is_human
    assert _is_toll_free(PhoneNumber("8448984442")) and not _is_toll_free(PhoneNumber("+14155551234"))
    for address in ("x@calendar.luma-mail.com", "14153750504.1213@txt.voice.google.com", "reply@em.omnihotels.com", "hey@posthog.com", "failed-payments+acct_1@stripe.com"):
        assert _is_role_or_robot_address(EmailAddress(address)), address
    for address in ("ada@example.com", "hilary@example.com", "emily@e-corp.com"):
        assert not _is_role_or_robot_address(EmailAddress(address)), address
    assert not people["Sam Guest"].is_human
    # A relay alias on a real person's card stays attached but is never the address shown first.
    assert people["Mira Vale"].is_human and people["Mira Vale"].emails == ("ada@example.com", "ada@user.luma-mail.com")


def test_thread_judgments_name_warm_numbers_and_drop_cold_and_automated_ones(archive_store: ArchiveStore) -> None:
    chats = {"w": "+15550000001", "c": "+15550000002", "a": "+15550000003"}
    with archive_store.transaction():
        for chat_id, phone in chats.items():
            archive_store.upsert_beeper_chat(
                BeeperChatRow(chat_id=chat_id, account_id="im", network="iMessage", chat_type="single", title=phone, participants_json="[]", last_activity=utc(2026, 3, 4)),
                [
                    BeeperParticipantRow(chat_id=chat_id, participant_id="me", handle_key=HandleKey("phone:+10000000000"), full_name="the user", phone="+10000000000", username="", is_self=True),
                    BeeperParticipantRow(chat_id=chat_id, participant_id="p", handle_key=HandleKey(f"phone:{phone}"), full_name="", phone=phone, username="", is_self=False),
                ],
            )
            archive_store.upsert_beeper_messages([BeeperMessageRow(message_id=f"m-{chat_id}", chat_id=chat_id, timestamp=utc(2026, 3, 4), sender_id="me", sender_name="the user", is_sender=True, text="hey")])
        now = datetime.now(timezone.utc)
        archive_store.save_thread_judgments([
            ThreadJudgmentRow(handle_key=HandleKey("phone:+15550000001"), kind="warm", name="Vincent", who="old colleague", confidence=0.9, judged_at=now, model="m"),
            ThreadJudgmentRow(handle_key=HandleKey("phone:+15550000002"), kind="cold", name="", who="recruiter", confidence=0.8, judged_at=now, model="m"),
            ThreadJudgmentRow(handle_key=HandleKey("phone:+15550000003"), kind="automated", name="", who="delivery notices", confidence=0.95, judged_at=now, model="m"),
        ])

    by_phone = {p.phones[0]: p for p in resolve_people(archive_store, [WORK_ACCOUNT]) if p.phones}

    assert by_phone["+15550000001"].is_human and by_phone["+15550000001"].canonical_name == "Vincent"
    assert not by_phone["+15550000002"].is_human
    assert not by_phone["+15550000003"].is_human
