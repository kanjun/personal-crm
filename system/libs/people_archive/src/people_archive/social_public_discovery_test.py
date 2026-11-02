"""The public pilot retains originals and keeps ambiguous profiles separate."""

import json
import multiprocessing
import time

import pytest

from people_archive import social_public_discovery
from people_archive.primitives import PersonId
from people_archive.social_public_discovery import (
    ACTIVE_KEY,
    PILOT_LIMIT,
    USED_KEY,
    _save_results,
    run_public_pilot,
    run_public_pilot_locked,
)
from people_archive.store import ArchiveStore, ResolvedPersonRow


def _person(store: ArchiveStore) -> PersonId:
    pid = PersonId("b" * 32)
    store.replace_people(
        [
            ResolvedPersonRow(
                person_id=pid,
                canonical_name="Alice Smith",
                aliases=(),
                emails=("alice@example.com",),
                phones=(),
                handle_keys=("email:alice@example.com",),
                organization="Example Labs",
                title="Engineer",
                tier=1,
                is_human=True,
                is_colleague=True,
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
    return pid


def test_search_retains_raw_result_and_separates_ambiguous_match(
    archive_store: ArchiveStore,
) -> None:
    pid = _person(archive_store)
    archive_store.set_social_enrichment_metadata(USED_KEY, "1")
    active = {
        "person_id": str(pid),
        "name": "Alice Smith",
        "organization": "Example Labs",
        "platform": "linkedin",
        "query": '"Alice Smith" "Example Labs" site:linkedin.com/in',
        "snapshot_id": "sd_test",
    }
    raw = [
        {
            "organic": [
                {
                    "link": "https://www.linkedin.com/in/alicesmith",
                    "title": "Alice Smith - Example Labs",
                    "description": "Engineer at Example Labs",
                },
                {
                    "link": "https://www.linkedin.com/in/alice-smith",
                    "title": "Alice Smith - Professional Profile",
                    "description": "San Francisco Bay Area",
                },
            ]
        }
    ]
    result = _save_results(archive_store, active, raw)
    assert result["accepted"] == 1
    assert result["uncertain"] == 1
    profile = archive_store.social_profiles(pid)[0]
    assert profile.profile_url == "https://www.linkedin.com/in/alicesmith"
    source = archive_store.get_social_source(profile.source_id)
    assert source is not None
    assert source["raw"]["response"] == raw
    candidates = archive_store.execute_read(
        "SELECT state FROM social_profile_candidates WHERE person_id=? ORDER BY profile_url",
        [str(pid)],
    )
    assert sorted(state for (state,) in candidates) == ["accepted", "needs_review"]


def test_pilot_stops_at_durable_paid_input_limit(archive_store: ArchiveStore) -> None:
    archive_store.set_social_enrichment_metadata(USED_KEY, str(PILOT_LIMIT))
    assert run_public_pilot(archive_store) == {
        "state": "pilot_complete",
        "inputs_used": PILOT_LIMIT,
    }


def test_overlapping_runs_reserve_only_one_paid_input(
    archive_store: ArchiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _person(archive_store)
    context = multiprocessing.get_context("fork")
    first_request_started = context.Event()
    results = context.Queue()

    def fake_request(
        method: str, _url: str, _payload: object = None
    ) -> tuple[int, object]:
        if method == "POST":
            first_request_started.set()
            time.sleep(0.4)
            return 202, {"snapshot_id": "sd_one"}
        return 200, {"status": "running"}

    monkeypatch.setattr(social_public_discovery, "_request", fake_request)

    def worker() -> None:
        results.put(run_public_pilot_locked(archive_store.path))

    first = context.Process(target=worker)
    second = context.Process(target=worker)
    first.start()
    assert first_request_started.wait(5)
    second.start()
    first.join(10)
    second.join(10)
    assert first.exitcode == second.exitcode == 0
    assert results.get(timeout=1)["state"] == "pending"
    assert results.get(timeout=1)["state"] == "pending"
    assert archive_store.social_enrichment_metadata(USED_KEY) == "1"
    assert (
        json.loads(archive_store.social_enrichment_metadata(ACTIVE_KEY) or "{}")[
            "snapshot_id"
        ]
        == "sd_one"
    )


def test_failed_submission_response_is_saved_before_clearing_active(
    archive_store: ArchiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _person(archive_store)
    monkeypatch.setattr(
        social_public_discovery,
        "_request",
        lambda *_args: (429, {"error": "quota reached"}),
    )
    with pytest.raises(RuntimeError, match="HTTP 429"):
        run_public_pilot(archive_store)
    assert archive_store.social_enrichment_metadata(ACTIVE_KEY) == ""
    sources = archive_store.execute_read(
        "SELECT source_id FROM social_profile_sources WHERE person_id=? AND status='submission_error'",
        [str(pid)],
    )
    assert len(sources) == 1
    source = archive_store.get_social_source(str(sources[0][0]))
    assert source is not None
    assert source["raw"]["response"] == {"error": "quota reached"}


def test_failed_progress_response_is_saved_before_clearing_active(
    archive_store: ArchiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _person(archive_store)
    active = {
        "person_id": str(pid),
        "platform": "linkedin",
        "name": "Alice Smith",
        "organization": "Example Labs",
        "query": '"Alice Smith" "Example Labs" site:linkedin.com/in',
        "submitted_at": "2026-09-30T00:00:00+00:00",
        "snapshot_id": "sd_failed",
    }
    archive_store.set_social_enrichment_metadata(ACTIVE_KEY, json.dumps(active))
    monkeypatch.setattr(
        social_public_discovery,
        "_request",
        lambda *_args: (200, {"status": "failed", "reason": "provider failure"}),
    )
    assert run_public_pilot(archive_store)["state"] == "failed"
    assert archive_store.social_enrichment_metadata(ACTIVE_KEY) == ""
    sources = archive_store.execute_read(
        "SELECT source_id FROM social_profile_sources WHERE person_id=? AND status='failed'",
        [str(pid)],
    )
    assert len(sources) == 1
    source = archive_store.get_social_source(str(sources[0][0]))
    assert source is not None
    assert source["raw"]["response"] == {
        "status": "failed",
        "reason": "provider failure",
    }
