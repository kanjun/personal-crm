"""Acceptance requires a sender-owned link and a full-name username match."""

import pytest

from people_archive.primitives import PersonId
from people_archive.social_private_discovery import (
    _mentions_linked_person_as_third_party,
    own_message_text,
    run_private_discovery,
)
from people_archive.store import ArchiveStore, ResolvedPersonRow


def test_own_message_text_excludes_quoted_profiles() -> None:
    text = (
        "Thanks, see you soon.\n\n"
        "On Mon, Sep 28, 2026 at 12:00 PM, Chris Doe wrote:\n"
        "> https://x.com/alicesmith"
    )
    assert own_message_text(text).strip() == "Thanks, see you soon."


def test_introduction_link_is_ambiguous_even_with_a_matching_name() -> None:
    url = "https://www.linkedin.com/in/noravale"
    assert _mentions_linked_person_as_third_party(
        f"I wanted to introduce you to my good friend, Nora Vale <{url}>.", url
    )
    assert not _mentions_linked_person_as_third_party(
        f"Best, Nora Vale\nLinkedIn: {url}", url
    )


@pytest.mark.parametrize("is_colleague", [False, True])
def test_private_discovery_is_resumable_and_preserves_evidence(
    archive_store: ArchiveStore, is_colleague: bool
) -> None:
    pid = PersonId("a" * 32)
    archive_store.replace_people(
        [
            ResolvedPersonRow(
                person_id=pid,
                canonical_name="Alice Smith",
                aliases=(),
                emails=("alice@example.com",),
                phones=(),
                handle_keys=("email:alice@example.com",),
                organization="Example",
                title="",
                tier=1,
                is_human=True,
                is_colleague=is_colleague,
                hand_kept_category="",
                score=10,
                received_human_count=1,
                received_automated_count=0,
                sent_count=0,
                meeting_count=0,
                text_days_sent=0,
                text_days_received=0,
                first_contact=None,
                last_contact=None,
            )
        ],
        [],
    )
    messages = [
        (
            "m1",
            "Hello\nOn Mon, Sep 28, 2026 at 12:00 PM, Chris Doe wrote:\n> https://x.com/alicesmith",
        ),
        (
            "m2",
            "Best, Alice\nhttps://www.linkedin.com/in/alicesmith\nhttps://x.com/asmith",
        ),
    ]
    for message_id, body in messages:
        archive_store._connection.execute(
            "INSERT INTO messages (account, message_id, thread_id, ts, direction, from_name, "
            "from_addr, to_json, cc_json, subject, snippet, text, label_ids_json, "
            "is_automated, rfc_message_id, in_reply_to) "
            "VALUES ('owner@example.com', ?, ?, 1, 'RECEIVED', 'Alice Smith', "
            "'alice@example.com', '[]', '[]', '', '', ?, '[]', 0, '', '')",
            (message_id, message_id, body),
        )
    if is_colleague:
        # The main pass already scanned these messages under the old filter.
        archive_store.set_social_enrichment_metadata(
            "social_private_message_rowid", "2"
        )
    first = run_private_discovery(
        archive_store, limit=1, colleague_backfill=is_colleague
    )
    assert first["accepted"] == {"linkedin": 0, "x": 0, "instagram": 0}
    second = run_private_discovery(
        archive_store, limit=1, colleague_backfill=is_colleague
    )
    assert second["accepted"]["linkedin"] == 1
    assert second["candidates"]["x"] == 1
    profile = archive_store.social_profiles(pid)[0]
    assert profile.profile_url == "https://www.linkedin.com/in/alicesmith"
    source = archive_store.get_social_source(profile.source_id)
    assert source is not None
    assert source["raw"]["message_id"] == "m2"
    assert source["raw"]["text"] == messages[1][1]
    if is_colleague:
        assert (
            archive_store.social_enrichment_metadata("social_private_message_rowid")
            == "2"
        )
    assert run_private_discovery(archive_store, limit=10)["accepted"]["linkedin"] == 0
