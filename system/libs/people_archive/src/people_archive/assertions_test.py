from pathlib import Path

import pytest

from people_archive.assertions import (
    Decision,
    append_assertion,
    is_decided,
    load_assertions,
    make_assertion,
)
from people_archive.data_types import Direction
from people_archive.identity import find_merge_candidates, resolve_people
from people_archive.store import ArchiveStore
from people_archive.testing import WORK_ACCOUNT, make_parsed_email, utc


def _two_alex_chens(store: ArchiveStore) -> None:
    with store.transaction():
        store.upsert_message(make_parsed_email("c1", "alex.chen@one.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 1), "s", "t", False, "Alex Chen"))
        store.upsert_message(make_parsed_email("c2", "achen@two.com", ("owner@example.com",), Direction.RECEIVED, utc(2026, 1, 2), "s", "t", False, "Alex Chen"))


def test_same_decision_joins_two_people_on_the_next_pass(archive_store: ArchiveStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRM_DATA_DIR", str(tmp_path))
    _two_alex_chens(archive_store)
    assert len(resolve_people(archive_store, [WORK_ACCOUNT])) == 2

    append_assertion(tmp_path / "identity_assertions.jsonl", make_assertion(Decision.SAME, ["email:alex.chen@one.com"], ["email:achen@two.com"], note=""))
    people = resolve_people(archive_store, [WORK_ACCOUNT])

    assert len(people) == 1
    assert set(people[0].emails) == {"alex.chen@one.com", "achen@two.com"}


def test_different_decision_removes_the_pair_from_the_review_list(archive_store: ArchiveStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRM_DATA_DIR", str(tmp_path))
    _two_alex_chens(archive_store)
    assert len(find_merge_candidates(resolve_people(archive_store, [WORK_ACCOUNT]))) == 1

    append_assertion(tmp_path / "identity_assertions.jsonl", make_assertion(Decision.DIFFERENT, ["email:alex.chen@one.com"], ["email:achen@two.com"], note=""))

    assert find_merge_candidates(resolve_people(archive_store, [WORK_ACCOUNT])) == []


def test_ledger_round_trips_and_skips_bad_lines(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    append_assertion(path, make_assertion(Decision.SAME, ["email:a@x.com"], ["phone:+15550100001"], note="seen together"))
    path.open("a").write("not json\n")

    loaded = load_assertions(path)

    assert len(loaded) == 1 and loaded[0].decision == Decision.SAME and loaded[0].note == "seen together"


def test_is_decided_reads_same_broadly_and_different_by_side() -> None:
    same = make_assertion(Decision.SAME, ["email:a@x.com"], ["phone:+1"], note="")
    different = make_assertion(Decision.DIFFERENT, ["email:a@x.com"], ["email:b@y.com"], note="")

    # SAME joined a@x and +1: any pair drawn from that person is settled, whichever side each handle came from.
    assert is_decided(same, ["phone:+1", "email:new@x.com"], ["email:a@x.com"])
    assert not is_decided(same, ["email:a@x.com"], ["email:other@z.com"])
    # DIFFERENT settles only the two sides it kept apart, in either order.
    assert is_decided(different, ["email:b@y.com"], ["email:a@x.com"])
    assert not is_decided(different, ["email:a@x.com"], ["email:c@z.com"])
