import sqlite3
import subprocess
from datetime import datetime, timezone
from threading import Barrier

import pytest

from people_archive.primitives import PersonId
from people_archive.social_enrichment import (
    BrightDataClient,
    BrightDataError,
    BrightDataInactive,
    BrightDataPending,
    _person_discovery_context,
    canonical_profile_url,
    extract_profile_urls,
    parse_brightdata_profile,
    profile_username_matches_person,
    run_enrichment_pass,
    save_profile_observation,
)
from people_archive.store import ArchiveStore, ResolvedPersonRow, SocialProfileRow


def _resolved_person(
    person_id: str, *, human: bool, colleague: bool
) -> ResolvedPersonRow:
    return ResolvedPersonRow(
        person_id=PersonId(person_id),
        canonical_name="Sample Person",
        aliases=(),
        emails=(f"{person_id}@example.com",),
        phones=(),
        handle_keys=(),
        organization="Example",
        title="",
        tier=2,
        is_human=human,
        is_colleague=colleague,
        hand_kept_category="",
        score=10,
        received_human_count=1,
        received_automated_count=0,
        sent_count=1,
        meeting_count=0,
        text_days_sent=0,
        text_days_received=0,
        first_contact=None,
        last_contact=None,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "https://www.linkedin.com/in/miravale/?trk=abc",
            ("linkedin", "https://www.linkedin.com/in/miravale", "miravale"),
        ),
        ("https://twitter.com/finchsample", ("x", "https://x.com/finchsample", "finchsample")),
        (
            "https://www.instagram.com/sariazout/",
            ("instagram", "https://www.instagram.com/sariazout", "sariazout"),
        ),
        ("https://x.com/finchsample/status/123", None),
        ("https://instagram.com/p/abc", None),
        ("https://notlinkedin.com/in/alicesmith", None),
        ("https://x.com/alicesmith__;!!tracking", None),
        (
            "https://dk.linkedin.com/in/Mira-v%C3%A4le-101010101",
            (
                "linkedin",
                "https://www.linkedin.com/in/Mira-v%C3%A4le-101010101",
                "Mira-v%C3%A4le-101010101",
            ),
        ),
        ("https://www.linkedin.com/in/bad%2Fslug", None),
    ],
)
def test_canonical_profile_url(
    value: str, expected: tuple[str, str, str] | None
) -> None:
    assert canonical_profile_url(value) == expected


def test_extract_profile_urls_ignores_posts_and_deduplicates() -> None:
    text = (
        "LinkedIn https://linkedin.com/in/miravale and again https://www.linkedin.com/in/miravale/?x=1. "
        "Tweet https://x.com/finchsample/status/123 profile https://twitter.com/finchsample."
    )
    assert extract_profile_urls(text) == (
        ("linkedin", "https://www.linkedin.com/in/miravale", "miravale"),
        ("x", "https://x.com/finchsample", "finchsample"),
    )


@pytest.mark.parametrize(
    ("platform", "url", "raw", "followers", "following", "connections"),
    [
        (
            "x",
            "https://x.com/finchsample",
            {
                "url": "https://x.com/finchsample",
                "profile_name": "Finch",
                "followers": "38K",
                "following": 925,
            },
            38_000,
            925,
            None,
        ),
        (
            "instagram",
            "https://instagram.com/sari",
            {"account": "sari", "followers": "7,611", "following": 88},
            7_611,
            88,
            None,
        ),
        (
            "linkedin",
            "https://linkedin.com/in/miravale",
            {"name": "Sari Azout", "connections": "500+", "followers_count": 7000},
            7000,
            None,
            500,
        ),
    ],
)
def test_parse_brightdata_profile(
    platform: str,
    url: str,
    raw: dict[str, object],
    followers: int | None,
    following: int | None,
    connections: int | None,
) -> None:
    parsed = parse_brightdata_profile(platform, url, raw)
    assert (parsed.follower_count, parsed.following_count, parsed.connection_count) == (
        followers,
        following,
        connections,
    )


def test_brightdata_inactive_is_a_distinct_pause() -> None:
    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, '"Customer is not active"\n400', "")

    with pytest.raises(BrightDataInactive):
        BrightDataClient(runner).scrape("x", "https://x.com/finchsample")


def test_brightdata_202_keeps_the_snapshot_id() -> None:
    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [], 0, '{"snapshot_id":"s_example123"}\n202', ""
        )

    with pytest.raises(BrightDataPending) as raised:
        BrightDataClient(runner).scrape("x", "https://x.com/finchsample")
    assert raised.value.snapshot_id == "s_example123"


def test_brightdata_timeout_becomes_an_uncertain_request() -> None:
    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["curl"], 90)

    with pytest.raises(BrightDataError, match="did not complete"):
        BrightDataClient(runner).scrape("x", "https://x.com/sampleperson")


def test_concurrent_count_fetches_preserve_each_raw_response(
    archive_store: ArchiveStore,
) -> None:
    people = [PersonId(str(number) * 32) for number in (1, 2, 3)]
    archive_store.replace_people(
        [_resolved_person(str(pid), human=True, colleague=False) for pid in people], []
    )
    for number, pid in enumerate(people, start=1):
        archive_store.save_social_profile(
            SocialProfileRow(
                person_id=pid,
                platform="x",
                profile_url=f"https://x.com/sampleperson{number}",
                username=f"sampleperson{number}",
                confidence=1.0,
                match_reason="Known profile.",
                discovered_via="crm_handle",
                observed_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
            )
        )

    barrier = Barrier(3)

    class ConcurrentClient:
        def scrape(self, _platform: str, url: str) -> tuple[dict[str, object], int]:
            barrier.wait(timeout=3)
            return {"url": url, "followers": 42}, 200

    summary = run_enrichment_pass(
        archive_store, ConcurrentClient(), limit=3, harvest_known=False, workers=3
    )
    assert summary.profiles_fetched == 3
    assert archive_store.pending_social_fetches(3) == ()
    for pid in people:
        profile = archive_store.social_profiles(pid)[0]
        assert profile.follower_count == 42
        source = archive_store.get_social_source(profile.source_id)
        assert source is not None
        assert source["raw"]["followers"] == 42


def test_malformed_profile_does_not_discard_other_concurrent_responses(
    archive_store: ArchiveStore,
) -> None:
    invalid_person = PersonId("7" * 32)
    good_person = PersonId("8" * 32)
    archive_store.replace_people(
        [
            _resolved_person(str(invalid_person), human=True, colleague=False),
            _resolved_person(str(good_person), human=True, colleague=False),
        ],
        [],
    )
    for pid, platform, url in (
        (invalid_person, "linkedin", "https://www.linkedin.com/in/bad%2Fslug"),
        (good_person, "x", "https://x.com/goodperson"),
    ):
        archive_store.save_social_profile(
            SocialProfileRow(
                person_id=pid,
                platform=platform,
                profile_url=url,
                username=url.rsplit("/", 1)[-1],
                confidence=1.0,
                match_reason="Known profile.",
                discovered_via="crm_handle",
                observed_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
            )
        )

    barrier = Barrier(2)

    class MixedClient:
        def scrape(self, platform: str, url: str) -> tuple[dict[str, object], int]:
            barrier.wait(timeout=3)
            if platform == "linkedin":
                return {"url": url, "followers": 99}, 200
            return {"url": url, "followers": 42}, 200

    summary = run_enrichment_pass(
        archive_store, MixedClient(), limit=2, harvest_known=False, workers=2
    )
    assert (summary.profiles_fetched, summary.profiles_errored) == (1, 1)
    assert archive_store.social_profiles(good_person)[0].follower_count == 42
    source = archive_store.execute_read(
        "SELECT source_id, status FROM social_profile_sources WHERE person_id=?",
        [str(invalid_person)],
    )
    assert len(source) == 1 and source[0][1] == "invalid_profile"
    preserved = archive_store.get_social_source(source[0][0])
    assert preserved is not None
    assert preserved["raw"] == {
        "url": "https://www.linkedin.com/in/bad%2Fslug",
        "followers": 99,
    }
    assert archive_store.execute_read(
        "SELECT COUNT(*) FROM social_profile_sources WHERE status='requesting'", []
    ) == [(0,)]


def test_uncertain_brightdata_request_is_not_resubmitted(
    archive_store: ArchiveStore,
) -> None:
    pid = PersonId("4" * 32)
    archive_store.replace_people(
        [_resolved_person(str(pid), human=True, colleague=False)], []
    )
    archive_store.save_social_profile(
        SocialProfileRow(
            person_id=pid,
            platform="x",
            profile_url="https://x.com/sampleperson4",
            username="sampleperson4",
            confidence=1.0,
            match_reason="Known profile.",
            discovered_via="crm_handle",
            observed_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )
    )

    class UncertainClient:
        calls = 0

        def scrape(self, _platform: str, _url: str) -> tuple[dict[str, object], int]:
            self.calls += 1
            raise BrightDataError("network timeout")

    client = UncertainClient()
    run_enrichment_pass(archive_store, client, limit=1, harvest_known=False)
    run_enrichment_pass(archive_store, client, limit=1, harvest_known=False)
    assert client.calls == 1
    assert archive_store.execute_read(
        "SELECT status FROM social_profile_sources WHERE person_id=?", [str(pid)]
    ) == [("uncertain",)]


def test_returned_canonical_url_keeps_request_evidence_and_avoids_refetch(
    archive_store: ArchiveStore,
) -> None:
    pid = PersonId("5" * 32)
    archive_store.replace_people(
        [_resolved_person(str(pid), human=True, colleague=False)], []
    )
    archive_store.save_social_profile(
        SocialProfileRow(
            person_id=pid,
            platform="x",
            profile_url="https://x.com/oldname",
            username="oldname",
            confidence=1.0,
            match_reason="Known profile.",
            discovered_via="crm_handle",
            observed_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )
    )

    class RedirectingClient:
        calls = 0

        def scrape(self, _platform: str, _url: str) -> tuple[dict[str, object], int]:
            self.calls += 1
            return {"url": "https://x.com/newname", "followers": 99}, 200

    client = RedirectingClient()
    first = run_enrichment_pass(archive_store, client, limit=1, harvest_known=False)
    second = run_enrichment_pass(archive_store, client, limit=1, harvest_known=False)
    assert first.profiles_fetched == 1
    assert second.profiles_fetched == 0
    assert client.calls == 1
    profile = archive_store.social_profiles(pid)[0]
    assert profile.profile_url == "https://x.com/newname"
    source = archive_store.get_social_source(profile.source_id)
    assert source is not None
    assert source["source_url"] == "https://x.com/oldname"
    assert source["raw"]["url"] == "https://x.com/newname"


def test_delayed_brightdata_response_is_retrieved_without_a_second_scrape(
    archive_store: ArchiveStore,
) -> None:
    person_id = PersonId("6" * 32)
    archive_store.replace_people(
        [_resolved_person(str(person_id), human=True, colleague=False)], []
    )
    archive_store.save_social_profile(
        SocialProfileRow(
            person_id=person_id,
            platform="x",
            profile_url="https://x.com/sampleperson",
            username="sampleperson",
            confidence=0.99,
            match_reason="Contact card link.",
            discovered_via="crm_handle",
            observed_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )
    )

    class DelayedClient:
        scrapes = 0
        retrievals = 0

        def scrape(self, _platform: str, _url: str) -> tuple[dict[str, str], int]:
            self.scrapes += 1
            raise BrightDataPending("s_example123")

        def retrieve_snapshot(self, snapshot_id: str) -> dict[str, object]:
            assert snapshot_id == "s_example123"
            self.retrievals += 1
            return {"url": "https://x.com/sampleperson", "followers": 1234}

    client = DelayedClient()
    assert run_enrichment_pass(archive_store, client, limit=1).profiles_fetched == 0
    assert client.scrapes == 1
    assert archive_store.execute_read(
        "SELECT status FROM social_profile_sources WHERE person_id=?", [str(person_id)]
    ) == [("pending_snapshot",)]
    assert run_enrichment_pass(archive_store, client, limit=1).profiles_fetched == 1
    assert client.scrapes == 1
    assert client.retrievals == 1
    assert archive_store.social_profiles(person_id)[0].follower_count == 1234
    assert archive_store.execute_read(
        "SELECT status FROM social_profile_sources WHERE person_id=? ORDER BY fetched_at",
        [str(person_id)],
    ) == [("complete",), ("fetched",)]


def test_network_enrichment_includes_colleagues_and_does_not_repeat_a_failed_url(
    archive_store: ArchiveStore,
) -> None:
    colleague = PersonId("4" * 32)
    bot = PersonId("5" * 32)
    archive_store.replace_people(
        [
            _resolved_person(str(colleague), human=True, colleague=True),
            _resolved_person(str(bot), human=False, colleague=False),
        ],
        [],
    )
    archive_store.save_social_profile(
        SocialProfileRow(
            person_id=colleague,
            platform="x",
            profile_url="https://x.com/sampleperson",
            username="sampleperson",
            confidence=0.99,
            match_reason="Contact card link.",
            discovered_via="crm_handle",
            observed_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )
    )
    assert (
        archive_store.refresh_social_enrichment_queue(
            datetime(2026, 9, 30, tzinfo=timezone.utc)
        )
        == 1
    )
    assert [row[0] for row in archive_store.pending_social_discoveries(5)] == [
        str(colleague)
    ]

    class DeadPageClient:
        def scrape(self, _platform: str, _url: str) -> tuple[dict[str, str], int]:
            return {"error_code": "dead_page", "error": "Profile not found"}, 200

    summary = run_enrichment_pass(archive_store, DeadPageClient(), limit=1)
    assert summary.profiles_fetched == 0
    assert archive_store.pending_social_fetches(1) == ()
    assert archive_store.social_profiles(colleague)[0].follower_count is None
    source = archive_store.execute_read(
        "SELECT source_id, status FROM social_profile_sources WHERE person_id=?",
        [str(colleague)],
    )
    assert len(source) == 1 and source[0][1] == "error"
    assert (
        archive_store.get_social_source(source[0][0])["raw"]["error_code"]
        == "dead_page"
    )


def test_counts_only_fetches_known_profile_without_harvesting(
    archive_store: ArchiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    person_id = PersonId("7" * 32)
    archive_store.replace_people(
        [_resolved_person(str(person_id), human=True, colleague=False)], []
    )
    archive_store.save_social_profile(
        SocialProfileRow(
            person_id=person_id,
            platform="x",
            profile_url="https://x.com/sampleperson",
            username="sampleperson",
            confidence=0.99,
            match_reason="Contact card link.",
            discovered_via="crm_handle",
            observed_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )
    )
    archive_store.set_social_enrichment_metadata(
        "social_harvest_after", "2020-01-01T00:00:00+00:00"
    )

    def unexpected_harvest(*_args: object) -> None:
        raise AssertionError("Counts-only run searched for handles")

    monkeypatch.setattr(
        "people_archive.social_enrichment.harvest_known_profiles", unexpected_harvest
    )

    class ProfileClient:
        def scrape(self, _platform: str, _url: str) -> tuple[dict[str, object], int]:
            return {"url": "https://x.com/sampleperson", "followers": 1234}, 200

    summary = run_enrichment_pass(
        archive_store, ProfileClient(), limit=1, harvest_known=False
    )
    assert summary.profiles_fetched == 1
    assert archive_store.social_profiles(person_id)[0].follower_count == 1234
    assert (
        archive_store.social_enrichment_metadata("social_harvest_after")
        == "2020-01-01T00:00:00+00:00"
    )


def test_pending_fetches_ignore_profile_url_capitalization(
    archive_store: ArchiveStore,
) -> None:
    person_id = PersonId("8" * 32)
    archive_store.replace_people(
        [_resolved_person(str(person_id), human=True, colleague=False)], []
    )
    archive_store.save_social_profile(
        SocialProfileRow(
            person_id=person_id,
            platform="linkedin",
            profile_url="https://www.linkedin.com/in/SamplePerson",
            username="SamplePerson",
            confidence=0.99,
            match_reason="Contact card link.",
            discovered_via="crm_handle",
            observed_at=datetime.now(timezone.utc),
        )
    )
    archive_store.save_social_source(
        source_id="existing-scrape",
        person_id=person_id,
        platform="linkedin",
        source_url="https://www.linkedin.com/in/sampleperson",
        provider="brightdata",
        fetched_at=datetime.now(timezone.utc),
        status="fetched",
        raw={"followers_count": 1234},
    )
    assert archive_store.pending_social_fetches(1) == ()


@pytest.mark.parametrize(
    ("name", "aliases", "username", "expected"),
    [
        ("Sari Azout", '["Mira Vale"]', "sariazout", True),
        ("Ali Rohde", '["Ali R"]', "Julian", False),
        ("Finch Rowan", "[]", "finchsample", False),
        ("Avery Koonin", '["Avery Benjamin Koonin"]', "averykoonin", True),
    ],
)
def test_signature_username_requires_full_identity_match(
    name: str,
    aliases: str,
    username: str,
    expected: bool,
) -> None:
    assert profile_username_matches_person(name, aliases, username) is expected


def test_profile_observation_keeps_projection_and_raw_source(
    archive_store: ArchiveStore,
) -> None:
    person_id = PersonId("1" * 32)
    row = save_profile_observation(
        archive_store,
        person_id=person_id,
        platform="x",
        profile_url="https://x.com/finchsample",
        raw={
            "url": "https://x.com/finchsample",
            "profile_name": "Finch Rowan",
            "followers": 38_000,
        },
        provider="public_web",
        confidence=0.99,
        match_reason="Name, company, and biography agree.",
        discovered_via="public_search",
        observed_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
    )
    stored = archive_store.social_profiles(person_id)
    source = archive_store.get_social_source(row.source_id)
    assert stored[0].follower_count == 38_000
    assert source is not None and source["raw"]["profile_name"] == "Finch Rowan"


def test_signature_refresh_preserves_enriched_profile(
    archive_store: ArchiveStore,
) -> None:
    person_id = PersonId("2" * 32)
    observed_at = datetime(2026, 9, 24, tzinfo=timezone.utc)
    archive_store.upsert_social_candidate(
        person_id=person_id,
        platform="x",
        profile_url="https://x.com/example",
        username="example",
        discovered_via="email_signature",
        confidence=0.96,
        reason="Repeated matching signature.",
        state="accepted",
        seen_at=observed_at,
    )
    archive_store.save_social_profile(
        SocialProfileRow(
            person_id=person_id,
            platform="x",
            profile_url="https://x.com/example",
            username="example",
            follower_count=12_345,
            confidence=0.96,
            match_reason="Repeated matching signature.",
            discovered_via="email_signature",
            observed_at=observed_at,
        )
    )

    archive_store.reset_email_signature_social_candidates()

    assert archive_store.social_profiles(person_id)[0].follower_count == 12_345
    assert archive_store.execute_read(
        "SELECT COUNT(*) FROM social_profile_candidates WHERE person_id=?",
        [str(person_id)],
    ) == [(0,)]


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("missing search index"),
        ValueError("invalid event identifier or JSON"),
        TypeError("null attendee JSON"),
        IndexError("empty attendee"),
        KeyError(0),
        OverflowError("timestamp out of range"),
        OSError("timestamp unavailable"),
    ],
)
def test_optional_calendar_failure_retains_identity_context(
    archive_store, monkeypatch, error
):
    def unavailable(_self, _query, _limit):
        raise error

    monkeypatch.setattr(ArchiveStore, "search_events", unavailable)
    context = _person_discovery_context(
        archive_store,
        (
            "3" * 32,
            "A Person",
            '["A. Person"]',
            '["person@example.com"]',
            "Their organization",
            "Their title",
            1,
            25,
        ),
    )
    assert context["calendar_titles"] == []
    assert context["name"] == "A Person"
    assert context["aliases"] == ["A. Person"]
    assert context["emails"] == ["person@example.com"]
    assert context["organization"] == "Their organization"
    assert context["relationship_score"] == 25
