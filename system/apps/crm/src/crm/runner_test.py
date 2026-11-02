from datetime import datetime, timezone

from flask.testing import FlaskClient
from people_archive.data_types import Direction
from people_archive.identity import resolve_and_store_people
from people_archive.primitives import HandleKey
from people_archive.store import (
    ArchiveStore,
    BeeperChatRow,
    BeeperMessageRow,
    BeeperParticipantRow,
    MergeJudgmentRow,
    ThreadJudgmentRow,
    open_archive_store,
)
from people_archive.testing import WORK_ACCOUNT, make_parsed_email, utc

from crm import runner
from crm.queries import HeaderCounts


def test_people_page_lists_humans_and_hides_robots(client: FlaskClient) -> None:
    body = client.get("/").get_data(as_text=True)

    assert "Mira Vale" in body
    assert "no-reply@shop.com" not in body


def test_people_page_filters_by_query_and_tier(client: FlaskClient) -> None:
    assert "Mira Vale" in client.get("/?q=workshop").get_data(as_text=True)
    assert "No one matches" in client.get("/?q=nobody-here").get_data(as_text=True)
    assert "No one matches" in client.get("/?tier=3").get_data(as_text=True)


def test_person_page_shows_timeline_with_gmail_links(client: FlaskClient) -> None:
    people = client.get("/").get_data(as_text=True)
    person_href = next(line for line in people.split('"') if line.startswith("/person/"))

    body = client.get(person_href).get_data(as_text=True)

    assert "Engine plan 7" in body
    assert "mail.google.com" in body
    assert "+15550100001" in body


def test_search_page_finds_body_text_and_links_to_the_person(client: FlaskClient) -> None:
    body = client.get("/search?q=difference").get_data(as_text=True)

    assert "8 hits" in body
    assert "<mark>difference</mark>" in body
    # Mails the user sent link the recipient, not just the sender.
    assert body.count('class="plink"') == 8 and ">Mira Vale</a>" in body


def test_record_page_shows_raw_json_or_404(client: FlaskClient) -> None:
    assert client.get("/record/GMAIL/owner@example.com/does-not-exist").status_code == 404


def test_health(client: FlaskClient) -> None:
    assert client.get("/health").get_json() == {"status": "ok"}


def test_merge_decision_is_recorded_and_redirects(client: FlaskClient, seeded_data_dir, monkeypatch) -> None:
    monkeypatch.setenv("CRM_SKIP_BACKGROUND_RESOLVE", "1")
    people = client.get("/").get_data(as_text=True)
    person_id = next(line for line in people.split('"') if line.startswith("/person/")).split("/")[-1]

    response = client.post("/merge", data={"a": person_id, "b": person_id, "decision": "different", "back": f"/person/{person_id}"})

    assert response.status_code == 302 and "queued=different" in response.headers["Location"]
    ledger = (seeded_data_dir / "identity_assertions.jsonl").read_text()
    assert '"decision":"DIFFERENT"' in ledger and "ada@example.com" in ledger


def test_decided_pair_leaves_the_duplicates_page_before_the_rebuild(client: FlaskClient, seeded_data_dir, monkeypatch) -> None:
    monkeypatch.setenv("CRM_SKIP_BACKGROUND_RESOLVE", "1")
    with open_archive_store(seeded_data_dir / "archive.sqlite") as store:
        with store.transaction():
            store.upsert_message(make_parsed_email("dup-1", "alex.chen@one.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 1), "hi", "hello there", False, "Alex Chen"))
            store.upsert_message(make_parsed_email("dup-2", "achen@two.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 2), "hi", "hello again", False, "Alex Chen"))
        resolve_and_store_people(store, [WORK_ACCOUNT])
        ids = [str(r[0]) for r in store.execute_read("SELECT person_id_a FROM merge_candidates UNION SELECT person_id_b FROM merge_candidates", [])]
    assert len(ids) == 2
    assert client.get("/merges").get_data(as_text=True).count("Alex Chen") == 2
    assert "Possibly the same person as" in client.get(f"/person/{ids[0]}").get_data(as_text=True)

    client.post("/merge", data={"a": ids[0], "b": ids[1], "decision": "same", "back": "/merges"})

    # The candidate table is untouched (no rebuild ran), yet the pair is gone from both pages.
    assert "Alex Chen" not in client.get("/merges").get_data(as_text=True)
    assert "Possibly the same person as" not in client.get(f"/person/{ids[0]}").get_data(as_text=True)


def test_duplicates_page_shows_the_judges_lean_and_what_it_settled(client: FlaskClient, seeded_data_dir, monkeypatch) -> None:
    monkeypatch.setenv("CRM_SKIP_BACKGROUND_RESOLVE", "1")
    with open_archive_store(seeded_data_dir / "archive.sqlite") as store:
        with store.transaction():
            for n, sender in enumerate(("alex.chen@one.com", "achen@two.com", "sam.lee@one.com", "slee@two.com")):
                name = "Alex Chen" if n < 2 else "Sam Lee"
                store.upsert_message(make_parsed_email(f"d{n}", sender, ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, n + 1), "hi", "hello", False, name))
        resolve_and_store_people(store, [WORK_ACCOUNT])
        judged_at = datetime.now(timezone.utc)
        store.save_merge_judgments([
            MergeJudgmentRow(pair_key="p1", handles_a=("email:alex.chen@one.com",), handles_b=("email:achen@two.com",), name="Alex Chen",
                             verdict="unsure", confidence=0.4, reason="common name, nothing links them", judged_at=judged_at, model="m"),
            MergeJudgmentRow(pair_key="p2", handles_a=("email:sam.lee@one.com",), handles_b=("email:slee@two.com",), name="Sam Lee",
                             verdict="same", confidence=0.9, reason="signature names the other address", judged_at=judged_at, model="m"),
        ])
    (seeded_data_dir / "identity_auto_decisions.jsonl").write_text(
        '{"decision":"SAME","handles_a":["email:sam.lee@one.com"],"handles_b":["email:slee@two.com"],"decided_at":"2026-09-23T00:00:00Z","note":"merge judge"}\n'
    )

    body = client.get("/merges").get_data(as_text=True)

    assert "1 pair for you" in body and "Judge unsure" in body and "common name, nothing links them" in body
    assert "Settled for you: 1 joined, 0 kept apart" in body and "signature names the other address" in body
    assert body.count("Sam Lee") == 1  # only in the settled list, not as a pair to decide


def test_in_place_edits_stick_and_show_as_your_words(client: FlaskClient) -> None:
    people = client.get("/").get_data(as_text=True)
    person_id = next(line for line in people.split('"') if line.startswith("/person/")).split("/")[-1]

    assert client.post(f"/person/{person_id}/edit", data={"field": "key_facts", "value": "Wrote the first program (1843 note)\nLoves engines"}).get_json() == {"ok": True}
    assert client.post(f"/person/{person_id}/edit", data={"field": "organization", "value": "Babbage & Co"}).status_code == 200
    assert client.post(f"/person/{person_id}/edit", data={"field": "interests", "value": "Mathematics, poetry"}).status_code == 200
    assert client.post(f"/person/{person_id}/edit", data={"field": "score", "value": "9"}).status_code == 400
    assert client.post("/person/nobody/edit", data={"field": "notes", "value": "x"}).status_code == 404

    body = client.get(f"/person/{person_id}").get_data(as_text=True)
    assert "<li>Wrote the first program" in body and "Loves engines" in body
    assert 'class="ed__you"' in body
    assert 'data-value="Babbage &amp; Co"' in body
    assert '<span class="tag">mathematics</span>' in body and '<span class="tag">poetry</span>' in body
    assert "Babbage &amp; Co" in client.get("/").get_data(as_text=True)


def test_people_page_groups_by_organization_and_role(client: FlaskClient) -> None:
    people = client.get("/").get_data(as_text=True)
    person_id = next(line for line in people.split('"') if line.startswith("/person/")).split("/")[-1]
    client.post(f"/person/{person_id}/edit", data={"field": "background", "value": "Engineer, writer"})

    by_organization = client.get("/?by=organization").get_data(as_text=True)
    by_role = client.get("/?by=role").get_data(as_text=True)
    one_role = client.get("/?by=role&in=writer").get_data(as_text=True)

    assert "<span>Example Workshop</span>" in by_organization and "Mira Vale" in by_organization
    assert "<span>engineer</span>" in by_role and "<span>writer</span>" in by_role
    assert "Mira Vale" in one_role and "<span>engineer</span>" not in one_role
    assert "Nothing to group yet" in client.get("/?by=interest&group=colleagues").get_data(as_text=True)


def test_people_page_computes_header_counts_once_per_ttl(client: FlaskClient, monkeypatch) -> None:
    calls: list[int] = []
    original = runner.header_counts

    def counted(store: ArchiveStore) -> HeaderCounts:
        calls.append(1)
        return original(store)

    monkeypatch.setattr(runner, "header_counts", counted)

    first = client.get("/").get_data(as_text=True)
    second = client.get("/?group=network").get_data(as_text=True)

    assert "calendar events" in first and "calendar events" in second
    assert len(calls) == 1


def test_number_only_person_shows_what_the_thread_said(client: FlaskClient, seeded_data_dir) -> None:
    with open_archive_store(seeded_data_dir / "archive.sqlite") as store:
        store.save_thread_judgments([ThreadJudgmentRow(
            handle_key=HandleKey("phone:+15550100001"), kind="warm", name="", who="ran the analytical engine workshop with the user",
            confidence=0.9, judged_at=datetime(2026, 3, 1, tzinfo=timezone.utc), model="test",
        )])

    people = client.get("/").get_data(as_text=True)
    person_href = next(line for line in people.split('"') if line.startswith("/person/"))
    person = client.get(person_href).get_data(as_text=True)

    assert "ran the analytical engine workshop with the user" in people
    assert "From your texts: ran the analytical engine workshop with the user" in person


def test_person_text_card_expands_to_full_archived_conversation(
    client: FlaskClient, seeded_data_dir
) -> None:
    chat_id = "imsg##thread:example"
    with open_archive_store(seeded_data_dir / "archive.sqlite") as store:
        person_id = str(store.execute_read(
            "SELECT person_id FROM person_handles WHERE handle_key=?",
            ["phone:+15550100001"],
        )[0][0])
        with store.transaction():
            store.upsert_beeper_chat(
                BeeperChatRow(
                    chat_id=chat_id,
                    account_id="imessage-example",
                    network="iMessage",
                    chat_type="single",
                    title="Ada",
                    participants_json="[]",
                    last_activity=utc(2026, 2, 3),
                ),
                [BeeperParticipantRow(
                    chat_id=chat_id,
                    participant_id="ada",
                    handle_key=HandleKey("phone:+15550100001"),
                    full_name="Ada",
                    phone="+15550100001",
                    username="",
                    is_self=False,
                )],
            )
            store.upsert_beeper_messages([
                BeeperMessageRow(message_id="m1", chat_id=chat_id, timestamp=utc(2026, 2, 1), sender_id="ada", sender_name="Ada", is_sender=False, text="First message"),
                BeeperMessageRow(message_id="m2", chat_id=chat_id, timestamp=utc(2026, 2, 2), sender_id="self", sender_name="You", is_sender=True, text="<b>Reply</b>"),
                BeeperMessageRow(message_id="m3", chat_id=chat_id, timestamp=utc(2026, 2, 3), sender_id="ada", sender_name="Ada", is_sender=False, text=""),
            ])

    person = client.get(f"/person/{person_id}").get_data(as_text=True)
    response = client.get(f"/person/{person_id}/chat", query_string={"chat_id": chat_id})
    thread = response.get_data(as_text=True)

    assert "3 messages" in person and "View full thread" in person
    assert response.status_code == 200
    assert thread.count('role="listitem"') == 3
    assert thread.index("First message") < thread.index("&lt;b&gt;Reply&lt;/b&gt;")
    assert "[Attachment or non-text message]" in thread
    assert client.get("/person/unknown/chat", query_string={"chat_id": chat_id}).status_code == 404
