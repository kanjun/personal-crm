import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from people_archive import gmail_store
from people_archive.gmail_fake import FakeGmail
from people_archive.gmail_store import GmailStore, StoreUnavailableError, log_fallback
from people_archive.gmail_sync import LAST_SUCCESS_KEY, sync_gmail_accounts
from people_archive.primitives import AccountEmail, SourceKind
from people_archive.store import ArchiveStore

ME = "owner@example.com"
_FAR = datetime(2100, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _unpaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("people_archive.gmail_sync.SYNC_START_RATE_PER_SECOND", 10_000.0)


def _synced(store: ArchiveStore, fake: FakeGmail, tmp_path: Path) -> GmailStore:
    with store.transaction():
        store.set_sync_value(SourceKind.GMAIL, AccountEmail(ME), "history_id", "100")
    sync_gmail_accounts(fake, store, [AccountEmail(ME)], _FAR, tmp_path / "i.jsonl", full_listing_max=50)
    return GmailStore(store.path)


def _mailbox() -> FakeGmail:
    fake = FakeGmail(history_id={ME: 100})
    # A thread the user started, with a reply from a third person.
    fake.add(ME, "a01", ["SENT"], sender="the user <owner@example.com>", to="Mike <mike@example.com>", thread_id="t1", body="lawyer?")
    fake.add(ME, "a02", ["INBOX", "UNREAD"], sender="Byron <byron@example.com>", to="mike@example.com", cc="owner@example.com", thread_id="t1")
    # A cold email, and a sent mail with the person only on Cc.
    fake.add(ME, "a03", ["INBOX"], sender="Cold <cold@example.com>", subject="pitch")
    fake.add(ME, "a04", ["SENT"], sender="owner@example.com", to="x@example.com", cc="Cara <cara@example.com>", body="invite https://studio.example/i/TOKEN123456789")
    fake.add(ME, "a05", ["SENT", "TRASH"], sender="owner@example.com", to="gone@example.com")
    fake.add(ME, "a08", ["SENT"], sender="owner@example.com", to="x@example.com", bcc="Quiet <quiet@example.com>")
    fake.add(ME, "a06", ["DRAFT"], sender="owner@example.com", to="cold@example.com")
    fake.add(ME, "a07", ["STARRED"])
    return fake


def test_inbox_and_metadata_come_back_in_gmails_shapes(archive_store: ArchiveStore, tmp_path: Path) -> None:
    with _synced(archive_store, _mailbox(), tmp_path) as reader:
        assert reader.inbox_listing(ME) == [{"id": "a03", "threadId": "a03"}, {"id": "a02", "threadId": "t1"}]
        meta = reader.metadata(ME, ["a02"], ["From", "Cc", "Subject"])["a02"]
        assert [h["name"] for h in meta["payload"]["headers"]] == ["From", "Subject", "Cc"]
        assert meta["labelIds"] == ["INBOX", "UNREAD"] and meta["threadId"] == "t1"
        assert "parts" not in meta["payload"] and "body" not in meta["payload"]


def test_sent_mail_questions_are_answered_locally(archive_store: ArchiveStore, tmp_path: Path) -> None:
    with _synced(archive_store, _mailbox(), tmp_path) as reader:
        assert reader.has_sent_to(ME, "Mike@Example.com")
        # Gmail's "to:" also counts Cc and Bcc.
        assert reader.has_sent_to(ME, "cara@example.com")
        assert reader.has_sent_to(ME, "quiet@example.com")
        # Trash is out of Gmail's search, and a draft was never sent.
        assert not reader.has_sent_to(ME, "gone@example.com")
        assert not reader.has_sent_to(ME, "cold@example.com")


def test_threads_hold_every_stored_message_oldest_first(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = _mailbox()
    with _synced(archive_store, fake, tmp_path) as reader:
        thread = reader.thread_metadata(ME, "t1", ["From"])
        assert thread is not None
        assert [m["id"] for m in thread["messages"]] == ["a01", "a02"]
        assert thread["messages"][0]["payload"]["headers"] == [{"name": "From", "value": "the user <owner@example.com>"}]
        assert reader.thread(ME, "missing") is None


def test_thread_labels_include_messages_known_only_by_their_labels(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = _mailbox()
    # An old muted message the archive never copied, on the cold sender's thread.
    fake.add(ME, "a00", ["Label_4"], thread_id="a03", is_recorded=False)
    with _synced(archive_store, fake, tmp_path) as reader:
        assert reader.thread_labels(ME, "a03") == [
            {"id": "a00", "threadId": "a03", "labelIds": ["Label_4"]},
            {"id": "a03", "threadId": "a03", "labelIds": ["INBOX"]},
        ]
        assert reader.thread(ME, "a03")["messages"][0]["id"] == "a03"


def test_sent_since_latest_with_and_sent_containing(archive_store: ArchiveStore, tmp_path: Path) -> None:
    with _synced(archive_store, _mailbox(), tmp_path) as reader:
        since = datetime.fromtimestamp(0, tz=timezone.utc)
        assert [m["id"] for m in reader.sent_since(ME, since, limit=10)] == ["a08", "a04", "a01"]
        assert [m["id"] for m in reader.sent_since(ME, since, limit=1)] == ["a08"]
        latest = reader.latest_with(ME, "cold@example.com", since)
        # The newer draft to the same person does not count.
        assert latest is not None and latest["id"] == "a03"
        assert [m["id"] for m in reader.sent_containing(ME, ["TOKEN123456789", "nope"], since)] == ["a04"]


def test_changes_since_a_cursor_and_after_pruning(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = _mailbox()
    reader = _synced(archive_store, fake, tmp_path)
    cursor = reader.latest_change(ME)
    fake.relabel(ME, "a03", remove=["INBOX"])
    sync_gmail_accounts(fake, archive_store, [AccountEmail(ME)], _FAR, tmp_path / "i.jsonl")
    assert [(c["id"], c["kind"]) for c in reader.changes_since(ME, cursor) or []] == [("a03", "labels")]
    with archive_store.transaction():
        archive_store.prune_gmail_changes(datetime.now(timezone.utc) + timedelta(days=1))
    fake.relabel(ME, "a02", remove=["UNREAD"])
    sync_gmail_accounts(fake, archive_store, [AccountEmail(ME)], _FAR, tmp_path / "i.jsonl")
    assert reader.changes_since(ME, 0) is None
    assert reader.changes_since(ME, reader.latest_change(ME) - 1) is not None
    reader.close()


def test_readiness_turns_stale_without_a_recent_sync(archive_store: ArchiveStore, tmp_path: Path) -> None:
    with _synced(archive_store, _mailbox(), tmp_path) as reader:
        assert reader.unusable_reason(ME, needs_labels=True) is None
        with archive_store.transaction():
            old = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
            archive_store.set_sync_value(SourceKind.GMAIL, AccountEmail(ME), LAST_SUCCESS_KEY, old)
        assert reader.unusable_reason(ME, needs_labels=False) == "last synced 40 min ago"
        assert reader.unusable_reason("nobody@example.com", needs_labels=False) == "never synced"


def test_a_message_without_a_label_row_drops_labels_listed_in_full(archive_store: ArchiveStore, tmp_path: Path) -> None:
    fake = _mailbox()
    reader = _synced(archive_store, fake, tmp_path)
    raw = dict(fake.mailboxes[ME]["a03"], id="a99", labelIds=["INBOX", "STARRED", "IMPORTANT"])
    with archive_store.transaction():
        archive_store.upsert_raw_record(SourceKind.GMAIL, AccountEmail(ME), "a99", raw, datetime.now(timezone.utc))
    # INBOX and STARRED are known in full (it would have a row if it had them); IMPORTANT is not.
    message = reader.message(ME, "a99")
    assert message is not None and message["labelIds"] == ["IMPORTANT"]
    reader.close()


def test_missing_archive_and_fallback_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_STORE_PATH", str(tmp_path / "crm" / "archive.sqlite"))
    with pytest.raises(StoreUnavailableError):
        GmailStore()
    log_fallback("classify", ME, "never synced")
    assert '"consumer": "classify"' in (tmp_path / "crm" / "logs" / "gmail-store-fallbacks.jsonl").read_text()


def test_catch_up_runs_the_sync_unless_it_just_ran(archive_store: ArchiveStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _synced(archive_store, _mailbox(), tmp_path).close()
    monkeypatch.setenv("GMAIL_STORE_PATH", str(archive_store.path))
    ran: list[list[str]] = []
    monkeypatch.setattr(gmail_store.subprocess, "run", lambda argv, **_kw: ran.append(argv) or subprocess.CompletedProcess(argv, 0))
    # Synced a moment ago: nothing to do. Forced (after the caller's own write): runs, history only.
    assert gmail_store.catch_up(ME)
    assert ran == []
    assert gmail_store.catch_up(ME, is_forced=True)
    assert ran[0][-7:] == ["--account", ME, "--history-only", "--wait", "60", "--time-limit", "60"]
    assert str(archive_store.path) in ran[0]
    monkeypatch.setenv("GMAIL_STORE_CATCH_UP", "0")
    assert not gmail_store.catch_up(ME, is_forced=True)
