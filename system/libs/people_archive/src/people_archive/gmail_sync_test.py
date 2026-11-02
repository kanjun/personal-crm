import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from people_archive.errors import (
    CredentialsExpiredError,
    GatewayRateLimitedError,
    UpstreamApiError,
)
from people_archive.gmail_fake import FakeGmail
from people_archive.gmail_parse import parse_gmail_message
from people_archive.gmail_store import GmailStore
from people_archive.gmail_sync import (
    BASELINE_KEY,
    HISTORY_ID_KEY,
    LAST_SUCCESS_KEY,
    RETRY_KEY,
    GmailSyncSummary,
    sync_gmail_accounts,
)
from people_archive.interfaces import GoogleApiGatewayInterface
from people_archive.primitives import AccountEmail, SourceKind
from people_archive.store import ArchiveStore

ACCOUNT = "owner@example.com"
OTHER = "other@example.com"
_FAR = datetime(2100, 1, 1, tzinfo=timezone.utc)


def _store_legacy(store: ArchiveStore, fake: FakeGmail, account: str, message_id: str) -> None:
    """A message the CRM's old pull stored: raw record and projection, no label row."""
    raw = dict(fake.mailboxes[account][message_id])
    at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with store.transaction():
        store.upsert_raw_record(SourceKind.GMAIL, AccountEmail(account), message_id, raw, at)
        store.upsert_message(parse_gmail_message(AccountEmail(account), raw))


def _seed_legacy_cursor(store: ArchiveStore, fake: FakeGmail, account: str) -> None:
    with store.transaction():
        store.set_sync_value(SourceKind.GMAIL, AccountEmail(account), "history_id", str(fake.history_id.get(account, 100)))


def _run(
    fake: GoogleApiGatewayInterface, store: ArchiveStore, tmp_path: Path, accounts: tuple[str, ...] = (ACCOUNT,), full_listing_max: int = 3
) -> list[GmailSyncSummary]:
    return sync_gmail_accounts(
        fake, store, [AccountEmail(a) for a in accounts], _FAR, tmp_path / "ingest.jsonl", full_listing_max=full_listing_max
    )


def _reader(store: ArchiveStore) -> GmailStore:
    return GmailStore(store.path)


def test_new_mail_is_stored_with_its_labels_and_reported_as_added(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = FakeGmail()
    _seed_legacy_cursor(archive_store, fake, ACCOUNT)
    fake.add(ACCOUNT, "a1", ["INBOX", "UNREAD"], subject="first")
    fake.add(ACCOUNT, "a2", ["INBOX"], subject="second")

    [summary] = _run(fake, archive_store, tmp_path)

    assert summary.is_caught_up and summary.fetched_count == 2
    with _reader(archive_store) as reader:
        assert reader.inbox_ids(ACCOUNT) == ["a2", "a1"]
        assert reader.message(ACCOUNT, "a1")["labelIds"] == ["INBOX", "UNREAD"]
        assert [(c["id"], c["kind"]) for c in reader.changes_since(ACCOUNT, 0) or []] == [("a1", "added"), ("a2", "added")]
        assert reader.readiness(ACCOUNT)["is_fresh"]
    assert archive_store.get_sync_value(SourceKind.GMAIL, AccountEmail(ACCOUNT), HISTORY_ID_KEY) == str(fake.history_id[ACCOUNT])


def test_archive_star_and_delete_in_gmail_show_up_locally(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = FakeGmail()
    _seed_legacy_cursor(archive_store, fake, ACCOUNT)
    for mid in ("b1", "b2", "b3"):
        fake.add(ACCOUNT, mid, ["INBOX", "UNREAD"])
    _run(fake, archive_store, tmp_path)
    with _reader(archive_store) as reader:
        cursor = reader.latest_change(ACCOUNT)

    fake.relabel(ACCOUNT, "b1", remove=["INBOX"])
    fake.relabel(ACCOUNT, "b2", add=["STARRED"], remove=["UNREAD"])
    fake.delete(ACCOUNT, "b3")
    [summary] = _run(fake, archive_store, tmp_path)

    assert summary.label_change_count == 3
    with _reader(archive_store) as reader:
        assert reader.inbox_ids(ACCOUNT) == fake.inbox_ids(ACCOUNT) == ["b2"]
        assert reader.message(ACCOUNT, "b2")["labelIds"] == ["INBOX", "STARRED"]
        assert reader.message(ACCOUNT, "b3") is None
        changes = reader.changes_since(ACCOUNT, cursor)
        assert [(c["id"], c["kind"]) for c in changes or []] == [("b1", "labels"), ("b2", "labels"), ("b3", "deleted")]


def test_baseline_corrects_labels_that_changed_before_the_sync_began(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = FakeGmail()
    # Stored by the old pull while in the inbox; since then archived, starred or read, unseen.
    for mid in ("c1", "c2", "c3", "c4"):
        fake.add(ACCOUNT, mid, ["INBOX", "UNREAD", "IMPORTANT"])
        _store_legacy(archive_store, fake, ACCOUNT, mid)
    fake.add(ACCOUNT, "c5", ["UNREAD", "IMPORTANT"])
    _store_legacy(archive_store, fake, ACCOUNT, "c5")
    _seed_legacy_cursor(archive_store, fake, ACCOUNT)
    fake.relabel(ACCOUNT, "c1", remove=["INBOX"], is_recorded=False)
    fake.relabel(ACCOUNT, "c2", add=["STARRED"], remove=["UNREAD"], is_recorded=False)
    fake.relabel(ACCOUNT, "c5", add=["STARRED"], is_recorded=False)
    # Old inbox mail the archive never stored (a bulk category the CRM's walk skipped).
    fake.add(ACCOUNT, "c0", ["INBOX", "CATEGORY_PROMOTIONS"], is_recorded=False)

    [summary] = _run(fake, archive_store, tmp_path, full_listing_max=3)

    assert summary.baseline_state == "done"
    assert summary.backfilled_count == 1
    with _reader(archive_store) as reader:
        assert reader.inbox_ids(ACCOUNT) == fake.inbox_ids(ACCOUNT) == ["c4", "c3", "c2", "c0"]
        for mid in ("c0", "c2", "c3", "c4"):
            # Every label on an inbox message is exact, small (STARRED) and large (UNREAD, IMPORTANT) alike.
            assert reader.message(ACCOUNT, mid)["labelIds"] == sorted(fake.mailboxes[ACCOUNT][mid]["labelIds"])
        # Out of the inbox, labels listed in full are exact; the large ones keep their stored value.
        assert "STARRED" in reader.message(ACCOUNT, "c5")["labelIds"]
        assert "INBOX" not in reader.message(ACCOUNT, "c1")["labelIds"]
        assert reader.readiness(ACCOUNT)["is_labels_ready"]


def test_stored_drafts_that_gmail_no_longer_has_are_marked_deleted(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = FakeGmail()
    fake.add(ACCOUNT, "c1", ["SENT"], sender="Owner <owner@example.com>", to="ada@example.com", thread_id="t1")
    fake.add(ACCOUNT, "c2", ["DRAFT"], sender="Owner <owner@example.com>", to="ada@example.com", thread_id="t1")
    fake.add(ACCOUNT, "c3", ["DRAFT"], sender="Owner <owner@example.com>", to="bob@example.com", thread_id="t3")
    for mid in ("c1", "c2", "c3"):
        _store_legacy(archive_store, fake, ACCOUNT, mid)
    _seed_legacy_cursor(archive_store, fake, ACCOUNT)
    # Sent (the draft's message went away) before the sync began, unseen by the old pull.
    fake.mailboxes[ACCOUNT].pop("c2")

    _run(fake, archive_store, tmp_path)

    with _reader(archive_store) as reader:
        assert reader.thread_message_ids(ACCOUNT, "t1") == ["c1"]
        assert reader.message(ACCOUNT, "c3")["labelIds"] == ["DRAFT"]


def test_recent_mail_deleted_before_the_sync_began_leaves_its_thread(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = FakeGmail()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    fake.add(ACCOUNT, "d1", ["Label_9"], thread_id="t1", internal_ms=now_ms - 60_000)
    fake.add(ACCOUNT, "d2", ["Label_9"], sender="reminder@superhuman.example", thread_id="t1", internal_ms=now_ms - 30_000)
    for mid in ("d1", "d2"):
        _store_legacy(archive_store, fake, ACCOUNT, mid)
    _seed_legacy_cursor(archive_store, fake, ACCOUNT)
    fake.mailboxes[ACCOUNT].pop("d2")

    # Label_9 is too big to list in full here, so only the check of recent mail can find d2 gone.
    _run(fake, archive_store, tmp_path, full_listing_max=0)

    with _reader(archive_store) as reader:
        assert reader.thread_message_ids(ACCOUNT, "t1") == ["d1"]
    assert json.loads(archive_store.get_sync_value(SourceKind.GMAIL, AccountEmail(ACCOUNT), BASELINE_KEY) or "{}")["is_recent_verified"]


def test_a_message_moved_back_into_the_inbox_is_fetched_when_not_stored(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = FakeGmail()
    fake.add(ACCOUNT, "d1", ["CATEGORY_UPDATES"], is_recorded=False)
    _seed_legacy_cursor(archive_store, fake, ACCOUNT)
    _run(fake, archive_store, tmp_path)

    fake.relabel(ACCOUNT, "d1", add=["INBOX"])
    _run(fake, archive_store, tmp_path)

    with _reader(archive_store) as reader:
        assert reader.inbox_ids(ACCOUNT) == ["d1"]
        assert reader.message(ACCOUNT, "d1")["labelIds"] == ["CATEGORY_UPDATES", "INBOX"]


def test_expired_history_relists_recent_mail_and_labels(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = FakeGmail()
    _seed_legacy_cursor(archive_store, fake, ACCOUNT)
    fake.add(ACCOUNT, "e1", ["INBOX"])
    _run(fake, archive_store, tmp_path)
    with archive_store.transaction():
        archive_store.set_sync_value(SourceKind.GMAIL, AccountEmail(ACCOUNT), LAST_SUCCESS_KEY, "2026-10-01T00:00:00+00:00")
    fake.add(ACCOUNT, "e2", ["INBOX"])
    fake.relabel(ACCOUNT, "e1", remove=["INBOX"])
    fake.expired_before[ACCOUNT] = fake.history_id[ACCOUNT]

    [recovering] = _run(fake, archive_store, tmp_path)
    [after] = _run(fake, archive_store, tmp_path)

    assert not recovering.is_caught_up
    assert after.is_caught_up and after.baseline_state == "done"
    with _reader(archive_store) as reader:
        assert reader.inbox_ids(ACCOUNT) == fake.inbox_ids(ACCOUNT) == ["e2"]


def test_a_run_stops_at_its_deadline_and_the_next_one_finishes(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = FakeGmail()
    for mid in ("f1", "f2", "f3", "f4", "f5", "f6"):
        fake.add(ACCOUNT, mid, ["INBOX"], is_recorded=False)
    _seed_legacy_cursor(archive_store, fake, ACCOUNT)
    start = datetime(2026, 10, 21, tzinfo=timezone.utc)
    ticks = iter(start + timedelta(seconds=i) for i in range(10_000))
    deadline = start + timedelta(seconds=12)

    [partial] = sync_gmail_accounts(fake, archive_store, [AccountEmail(ACCOUNT)], deadline, tmp_path / "i.jsonl", clock=lambda: next(ticks))

    assert partial.is_caught_up
    with _reader(archive_store) as reader:
        assert reader.missing_copies(ACCOUNT, reader.inbox_ids(ACCOUNT))
    [finished] = _run(fake, archive_store, tmp_path)
    assert finished.baseline_state == "done"
    with _reader(archive_store) as reader:
        assert reader.inbox_ids(ACCOUNT) == fake.inbox_ids(ACCOUNT)
        assert reader.missing_copies(ACCOUNT, reader.inbox_ids(ACCOUNT)) == []


def test_one_unreachable_account_does_not_stop_the_other(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = FakeGmail()
    _seed_legacy_cursor(archive_store, fake, ACCOUNT)
    fake.add(ACCOUNT, "9a1", ["INBOX"])
    # The other account has no history position and its history call fails like a lapsed login.
    broken = _BrokenFor(account=OTHER, inner=fake)

    summaries = sync_gmail_accounts(broken, archive_store, [AccountEmail(OTHER), AccountEmail(ACCOUNT)], _FAR, tmp_path / "i.jsonl")

    assert [s.is_caught_up for s in summaries] == [False, True]
    with _reader(archive_store) as reader:
        assert reader.inbox_ids(ACCOUNT) == ["9a1"]
        assert reader.unusable_reason(OTHER, needs_labels=False) == "never synced"


def test_failed_fetches_are_retried_next_run(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = FakeGmail()
    _seed_legacy_cursor(archive_store, fake, ACCOUNT)
    fake.add(ACCOUNT, "9b1", ["INBOX"])
    flaky = _FailingFetch(inner=fake, failing={"9b1"})
    _run(flaky, archive_store, tmp_path)
    assert json.loads(archive_store.get_sync_value(SourceKind.GMAIL, AccountEmail(ACCOUNT), RETRY_KEY) or "[]") == ["9b1"]

    _run(fake, archive_store, tmp_path)

    with _reader(archive_store) as reader:
        assert reader.message(ACCOUNT, "9b1") is not None
    assert json.loads(archive_store.get_sync_value(SourceKind.GMAIL, AccountEmail(ACCOUNT), BASELINE_KEY) or "{}")["is_done"]


def test_gmail_asking_to_slow_down_never_holds_a_run_past_its_deadline(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = FakeGmail()
    _seed_legacy_cursor(archive_store, fake, ACCOUNT)
    refusing = _AlwaysRateLimited(inner=fake)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=20)

    [summary] = sync_gmail_accounts(refusing, archive_store, [AccountEmail(ACCOUNT)], deadline, tmp_path / "i.jsonl")

    # One refusal pauses for a minute; the run gives up at once instead of waiting it out.
    assert not summary.is_caught_up
    assert datetime.now(timezone.utc) < deadline
    assert summary.request_count == 1


# --- helpers ---


class _AlwaysRateLimited(GoogleApiGatewayInterface):
    inner: FakeGmail = Field(description="Never reached")

    def get_json(self, account: AccountEmail, url: str, params: Mapping[str, str] | Sequence[tuple[str, str]]) -> dict[str, Any]:
        raise GatewayRateLimitedError("429 rateLimitExceeded")


class _BrokenFor(GoogleApiGatewayInterface):
    account: str = Field(description="The account whose calls fail")
    inner: FakeGmail = Field(description="Answers every other call")

    def get_json(self, account: AccountEmail, url: str, params: Mapping[str, str] | Sequence[tuple[str, str]]) -> dict[str, Any]:
        if str(account) == self.account:
            raise CredentialsExpiredError("credentials expired")
        return self.inner.get_json(account, url, params)


class _FailingFetch(GoogleApiGatewayInterface):
    inner: FakeGmail = Field(description="Answers every other call")
    failing: set[str] = Field(description="Message ids whose fetch fails")

    def get_json(self, account: AccountEmail, url: str, params: Mapping[str, str] | Sequence[tuple[str, str]]) -> dict[str, Any]:
        if any(url.endswith(f"/messages/{mid}") for mid in self.failing):
            raise UpstreamApiError("500 from Gmail: backend error")
        return self.inner.get_json(account, url, params)


@pytest.fixture(autouse=True)
def _unpaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real pacing (a few calls a second) would make every test here take many seconds."""
    monkeypatch.setattr("people_archive.gmail_sync.SYNC_START_RATE_PER_SECOND", 10_000.0)
