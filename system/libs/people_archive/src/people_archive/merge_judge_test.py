from datetime import datetime, timezone
from pathlib import Path

import pytest

from people_archive.assertions import Decision, load_assertions
from people_archive.data_types import Direction
from people_archive.identity import (
    find_merge_candidates,
    resolve_and_store_people,
    resolve_people,
)
from people_archive.merge_judge import (
    _pairs_to_judge,
    _parse,
    _signature_tail,
    decision_for,
    pair_key_for,
    write_auto_decisions,
)
from people_archive.store import ArchiveStore, MergeJudgmentRow
from people_archive.testing import WORK_ACCOUNT, make_parsed_email, utc


def _judgment(verdict: str, confidence: float) -> MergeJudgmentRow:
    return MergeJudgmentRow(
        pair_key=f"{verdict}-{confidence}", handles_a=("email:a@x.com",), handles_b=("email:b@y.com",), name="A B", verdict=verdict, confidence=confidence,
        reason="", judged_at=datetime.now(timezone.utc), model="m",
    )


def test_pair_key_ignores_side_and_handle_order() -> None:
    assert pair_key_for(["email:b", "email:a"], ["phone:1"]) == pair_key_for(["phone:1"], ["email:a", "email:b"])


def test_signature_tail_stops_at_quoted_history() -> None:
    text = "Thanks!\n\nSam Lee\nCTO, Acme\n\nOn Mon, Jan 1, the user wrote:\n> hi there"
    assert _signature_tail(text) == "Thanks! / Sam Lee / CTO, Acme"


def test_parse_reads_the_array_and_rejects_short_answers() -> None:
    lines = _parse('Sure: [{"n": 1, "verdict": "same", "confidence": 0.9, "reason": "r"}]', 1)
    assert lines[0].verdict == "same" and lines[0].confidence == 0.9
    with pytest.raises(Exception, match="Answered 1 of 2"):
        _parse('[{"n": 1, "verdict": "same"}]', 2)


def test_only_confident_verdicts_become_decisions() -> None:
    assert decision_for(_judgment("same", 0.7)) == Decision.SAME
    assert decision_for(_judgment("same", 0.65)) is None
    assert decision_for(_judgment("different", 0.8)) == Decision.DIFFERENT
    assert decision_for(_judgment("different", 0.75)) is None
    assert decision_for(_judgment("unsure", 1.0)) is None


def test_parse_accepts_a_bare_object_for_a_single_pair() -> None:
    assert _parse('{"n": 1, "verdict": "unsure", "confidence": 0.3, "reason": "r"}', 1)[0].verdict == "unsure"


def test_auto_ledger_is_rewritten_from_stored_judgments(archive_store: ArchiveStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRM_DATA_DIR", str(tmp_path))
    archive_store.save_merge_judgments([_judgment("same", 0.9), _judgment("unsure", 0.9)])

    assert write_auto_decisions(archive_store) == 1
    ledger = load_assertions(tmp_path / "identity_auto_decisions.jsonl")
    assert len(ledger) == 1 and ledger[0].decision == Decision.SAME and "merge judge" in ledger[0].note


def test_evidence_shows_signatures_and_shared_thread(archive_store: ArchiveStore) -> None:
    with archive_store.transaction():
        archive_store.upsert_message(make_parsed_email("t1", "alex.chen@one.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 1), "Intro", "Hi\n\nAlex Chen\nHead of Ops, One Inc", False, "Alex Chen"))
        archive_store.upsert_message(make_parsed_email("t2", "achen@two.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 2), "Re: Intro", "Replying from my new address", False, "Alex Chen"))
    resolve_and_store_people(archive_store, [WORK_ACCOUNT])

    pairs = _pairs_to_judge(archive_store, 10, rejudge=False)

    assert len(pairs) == 1
    assert "Head of Ops, One Inc" in pairs[0].side_a.evidence + pairs[0].side_b.evidence
    assert "no shared-record signals" in pairs[0].joint  # different Gmail threads in the fixture


def test_auto_ledger_joins_pairs_unless_user_split_them(archive_store: ArchiveStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRM_DATA_DIR", str(tmp_path))
    with archive_store.transaction():
        archive_store.upsert_message(make_parsed_email("c1", "alex.chen@one.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 1), "s", "t", False, "Alex Chen"))
        archive_store.upsert_message(make_parsed_email("c2", "achen@two.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 2), "s", "t", False, "Alex Chen"))
    assert len(resolve_people(archive_store, [WORK_ACCOUNT])) == 2

    (tmp_path / "identity_auto_decisions.jsonl").write_text(
        '{"decision":"SAME","handles_a":["email:alex.chen@one.com"],"handles_b":["email:achen@two.com"],"decided_at":"2026-09-23T00:00:00Z","note":"merge judge"}\n'
    )
    assert len(resolve_people(archive_store, [WORK_ACCOUNT])) == 1

    (tmp_path / "identity_assertions.jsonl").write_text(
        '{"decision":"DIFFERENT","handles_a":["email:alex.chen@one.com"],"handles_b":["email:achen@two.com"],"decided_at":"2026-09-23T00:00:00Z","note":""}\n'
    )
    people = resolve_people(archive_store, [WORK_ACCOUNT])
    assert len(people) == 2 and find_merge_candidates(people) == []


def test_auto_different_leaves_the_review_list(archive_store: ArchiveStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRM_DATA_DIR", str(tmp_path))
    with archive_store.transaction():
        archive_store.upsert_message(make_parsed_email("c1", "alex.chen@one.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 1), "s", "t", False, "Alex Chen"))
        archive_store.upsert_message(make_parsed_email("c2", "achen@two.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 2), "s", "t", False, "Alex Chen"))
    assert len(find_merge_candidates(resolve_people(archive_store, [WORK_ACCOUNT]))) == 1

    (tmp_path / "identity_auto_decisions.jsonl").write_text(
        '{"decision":"DIFFERENT","handles_a":["email:alex.chen@one.com"],"handles_b":["email:achen@two.com"],"decided_at":"2026-09-23T00:00:00Z","note":"merge judge"}\n'
    )

    assert find_merge_candidates(resolve_people(archive_store, [WORK_ACCOUNT])) == []
    assert len(load_assertions(tmp_path / "identity_auto_decisions.jsonl")) == 1
