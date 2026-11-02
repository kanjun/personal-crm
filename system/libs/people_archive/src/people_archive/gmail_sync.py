"""The one Gmail sync: keeps the archive's copy of each mailbox, and every message's labels, current.

Every few minutes each account asks Gmail's history "what changed since the last run" and applies
it: new messages are fetched in full and stored (raw record, projection, labels), label changes and
deletions update `gmail_label_state`, and each change is appended to `gmail_changes`, which the email
reviews and Today read instead of calling Gmail themselves (see gmail_store.py).

History only says what changed after tracking began, so each account also gets a one-time baseline
of its labels, done by listing (cheap: 500 ids a call) rather than fetching every message:

- the whole inbox, so the set of inbox messages is exact;
- every label with up to FULL_LISTING_MAX messages in full (starred, drafts, the Superhuman mute,
  most user labels), so those are exact on every message;
- the larger labels (unread, important, the categories) within the inbox, so every label on an
  inbox message is exact. Outside the inbox those few keep the value they had when fetched.

A message with no label row was stored before the sync and is in none of the labels listed in full
(it would have been listed); the reader relies on that.

Inbox mail the archive does not hold yet (the CRM's walk skipped the bulk categories) is fetched
newest first, a slice per run, and sent mail is checked for gaps once. The inbox list is compared
with Gmail's every RECONCILE_INTERVAL as a check that nothing was missed.

Everything is paced by one rate limiter per account, and each run stops starting new work at its
deadline; whatever is left continues next run.
"""

import json
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from loguru import logger
from pydantic import Field, PrivateAttr

from people_archive.data_types import GmailChangeKind, GmailLabelRow
from people_archive.errors import (
    CredentialsExpiredError,
    GatewayRateLimitedError,
    GatewayUnreachableError,
    PeopleArchiveError,
    UpstreamApiError,
)
from people_archive.gmail_parse import (
    bcc_addresses,
    label_row_from_raw,
    parse_gmail_message,
)
from people_archive.gmail_pull import GMAIL_BASE, log_ingest_problem
from people_archive.interfaces import GoogleApiGatewayInterface
from people_archive.primitives import (
    AccountEmail,
    GmailMessageId,
    GmailThreadId,
    SourceKind,
)
from people_archive.rate_limit import AdaptiveRateLimiter
from people_archive.store import ArchiveStore

HISTORY_ID_KEY: Final[str] = "sync_history_id"
LAST_SUCCESS_KEY: Final[str] = "sync_last_success"
# Labels whose membership is known for every message (INBOX once the inbox is listed, then each
# label listed in full); the reader drops these from a message with no label row.
FULL_KNOWN_LABELS_KEY: Final[str] = "labels_full_known"
BASELINE_KEY: Final[str] = "labels_baseline"
RECONCILED_AT_KEY: Final[str] = "inbox_reconciled_at"
RETRY_KEY: Final[str] = "fetch_retry"
# The CRM's pull kept its own history position; the sync starts from it so nothing between is missed.
_LEGACY_HISTORY_ID_KEY: Final[str] = "history_id"

FULL_LISTING_MAX: Final[int] = 15_000
RECONCILE_INTERVAL: Final[timedelta] = timedelta(hours=6)
# How far back stored mail is checked once for deletions that happened before the sync began.
RECENT_VERIFY_WINDOW: Final[timedelta] = timedelta(days=120)
CHANGE_FEED_RETENTION: Final[timedelta] = timedelta(days=30)
_LIST_PAGE_SIZE: Final[int] = 500
_HISTORY_PAGE_SIZE: Final[int] = 500
# More history than this in one run means the sync was away a long time: recover by listing instead.
_HISTORY_PAGES_MAX: Final[int] = 40
_FETCH_WORKERS: Final[int] = 3
_ATTEMPTS: Final[int] = 6
# The quota is shared with other readers; use a conservative two calls per second.
SYNC_START_RATE_PER_SECOND: Final[float] = 2.0
_WRITE_BATCH: Final[int] = 100
# After an expired history position, mail newer than the last success minus this is re-listed.
_RECOVERY_OVERLAP: Final[timedelta] = timedelta(days=1)
_INBOX: Final[str] = "INBOX"
_RETRY_MAX: Final[int] = 2000
# The error text of a fetch skipped because the run hit its deadline (not a failure worth logging).
_DEADLINE: Final[str] = "deadline"
# Never listed as labels: INBOX has its own pass, SENT never changes on a message.
_NOT_BASELINED: Final[frozenset[str]] = frozenset({"INBOX", "SENT"})


class OutOfTimeError(PeopleArchiveError):
    """The run reached its deadline; the work in progress resumes next run."""


class GmailSyncSummary(FrozenModel):
    """What one sync run did for one account."""

    account: AccountEmail = Field(description="Which mailbox")
    history_records: int = Field(description="History records read")
    added_count: int = Field(description="Messages Gmail reported added")
    fetched_count: int = Field(description="Messages fetched in full and stored")
    label_change_count: int = Field(description="Messages whose labels or deleted state changed")
    backfilled_count: int = Field(description="Inbox or sent messages fetched to fill gaps")
    inbox_mismatch_count: int = Field(description="Inbox differences the check found and fixed")
    baseline_state: str = Field(description="Where the one-time label baseline stands")
    is_caught_up: bool = Field(description="History was applied up to Gmail's current position")
    request_count: int = Field(description="Gmail requests made")


class _MessageEvent(FrozenModel):
    """One history entry about one message."""

    kind: str = Field(description="added, deleted, plus or minus")
    message_id: str = Field(description="Gmail message id")
    thread_id: str = Field(description="Gmail thread id")
    label_ids: tuple[str, ...] = Field(description="Labels added or removed (plus/minus); the snapshot for added")


class _Working(MutableModel):
    """A message's labels while a batch of history is applied."""

    thread_id: str = Field(description="Gmail thread id")
    internal_ms: int = Field(description="internalDate in ms, 0 when unknown")
    labels: set[str] = Field(description="Current labels")
    is_deleted: bool = Field(description="Deleted in Gmail")
    is_added: bool = Field(description="Gmail reported it added in this batch")
    needs_full: bool = Field(description="Must be fetched in full (not stored yet)")
    needs_labels: bool = Field(description="Stored but its labels must be read from Gmail")
    is_stored: bool = Field(description="The archive holds its full copy")
    before: tuple[tuple[str, ...], bool] | None = Field(description="(labels, deleted) before the batch; None if new")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AccountSync(MutableModel):
    """One account's sync for one run."""

    gateway: GoogleApiGatewayInterface = Field(frozen=True, description="Calls Gmail")
    store: ArchiveStore = Field(frozen=True, description="The archive")
    account: AccountEmail = Field(frozen=True, description="Mailbox being synced")
    deadline: datetime = Field(frozen=True, description="Stop starting new work after this")
    ingest_log: Path = Field(frozen=True, description="Where fetch failures are logged")
    clock: Callable[[], datetime] = Field(default=_now, frozen=True, description="The time now")
    full_listing_max: int = Field(default=FULL_LISTING_MAX, frozen=True, description="Largest label listed in full")
    limiter: AdaptiveRateLimiter = Field(
        default_factory=lambda: AdaptiveRateLimiter(rate_per_second=SYNC_START_RATE_PER_SECOND),
        description="Paces every call this run makes for the account",
    )
    _requests: int = PrivateAttr(default=0)
    _failed: set[str] = PrivateAttr(default_factory=set)
    _counts: dict[str, int] = PrivateAttr(default_factory=dict)

    # --- plumbing ---

    def _check_time(self) -> None:
        if self.clock() >= self.deadline:
            raise OutOfTimeError(f"Gmail sync for {self.account} reached its deadline")

    def _get(self, path: str, params: dict[str, str] | Sequence[tuple[str, str]]) -> dict[str, Any]:
        """One Gmail call, paced, retried while Gmail asks to slow down, never past the deadline."""
        for _ in range(_ATTEMPTS):
            self._check_time()
            if self.clock() + timedelta(seconds=self.limiter.paused_for_seconds()) >= self.deadline:
                raise OutOfTimeError(f"Gmail asked {self.account}'s sync to pause past its deadline")
            self.limiter.wait_for_slot()
            self._requests += 1
            try:
                body = self.gateway.get_json(self.account, f"{GMAIL_BASE}/{path}", params)
            except GatewayRateLimitedError as e:
                logger.info("Gmail {} asked to slow down: {}", self.account, str(e)[:200])
                self.limiter.record_rate_limited()
                continue
            self.limiter.record_success()
            return body
        raise GatewayRateLimitedError(f"Gmail kept refusing {path} for {self.account}")

    def _count(self, key: str, amount: int) -> None:
        self._counts[key] = self._counts.get(key, 0) + amount

    def _get_sync(self, key: str) -> str | None:
        return self.store.get_sync_value(SourceKind.GMAIL, self.account, key)

    def _set_sync(self, key: str, value: str) -> None:
        with self.store.transaction():
            self.store.set_sync_value(SourceKind.GMAIL, self.account, key, value)

    def _full_known_labels(self) -> set[str]:
        return set(json.loads(self._get_sync(FULL_KNOWN_LABELS_KEY) or "[]"))

    def _baseline(self) -> dict[str, Any]:
        return json.loads(self._get_sync(BASELINE_KEY) or "{}")

    def _save_baseline(self, baseline: dict[str, Any]) -> None:
        self._set_sync(BASELINE_KEY, json.dumps(baseline, sort_keys=True))

    def _fetch(self, message_id: str, message_format: str) -> tuple[str, dict[str, Any] | None, str | None]:
        """(id, message or None when Gmail no longer has it, error text or None)."""
        try:
            return message_id, self._get(f"messages/{message_id}", {"format": message_format}), None
        except UpstreamApiError as e:
            if "404" in str(e):
                return message_id, None, None
            return message_id, None, str(e)
        except GatewayRateLimitedError as e:
            return message_id, None, str(e)

    def _fetch_many(self, message_ids: Sequence[str], message_format: str) -> list[tuple[str, dict[str, Any] | None, str | None]]:
        if not message_ids:
            return []
        self._check_time()
        with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
            return list(pool.map(lambda mid: self._fetch_or_timeout(mid, message_format), message_ids))

    def _fetch_or_timeout(self, message_id: str, message_format: str) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            return self._fetch(message_id, message_format)
        except OutOfTimeError:
            return message_id, None, _DEADLINE

    def _note_failed(self, message_id: str, error: str) -> None:
        """A fetch that did not happen: retried at the start of the next run."""
        if error != _DEADLINE:
            log_ingest_problem(self.ingest_log, self.account, message_id, error)
        self._failed.add(message_id)

    def save_retries(self) -> None:
        pending = set(json.loads(self._get_sync(RETRY_KEY) or "[]")) | self._failed
        self._set_sync(RETRY_KEY, json.dumps(sorted(pending)[:_RETRY_MAX]))

    def retry_failed(self) -> None:
        """Fetch again the messages a previous run could not; they are new mail history already passed."""
        pending = json.loads(self._get_sync(RETRY_KEY) or "[]")
        if not pending:
            return
        missing = sorted(set(pending) - self.store.existing_record_ids(SourceKind.GMAIL, self.account, set(pending)))
        self._set_sync(RETRY_KEY, "[]")
        self._backfill(missing)

    def _store_full(self, raw: dict[str, Any], at: datetime) -> GmailLabelRow:
        self.store.upsert_raw_record(SourceKind.GMAIL, self.account, str(raw["id"]), raw, at)
        self.store.upsert_message(parse_gmail_message(self.account, raw))
        if "SENT" in (raw.get("labelIds") or []):
            self.store.save_sent_bcc(self.account, str(raw["id"]), bcc_addresses(raw))
        return label_row_from_raw(self.account, raw)

    # --- history ---

    def _start_history_id(self) -> str | None:
        return self._get_sync(HISTORY_ID_KEY) or self._get_sync(_LEGACY_HISTORY_ID_KEY)

    def apply_history(self) -> bool:
        """Apply every change since the saved position; False when the position had expired and
        the mailbox was re-listed instead. Raises OutOfTimeError if the deadline came first."""
        start = self._start_history_id()
        if start is None:
            self._recover(None)
            return False
        events: list[_MessageEvent] = []
        latest = start
        page_token = ""
        for _ in range(_HISTORY_PAGES_MAX):
            params = {"startHistoryId": start, "maxResults": str(_HISTORY_PAGE_SIZE)}
            if page_token:
                params["pageToken"] = page_token
            try:
                page = self._get("history", params)
            except UpstreamApiError as e:
                if "404" in str(e):
                    logger.warning("Gmail history for {} expired; re-listing recent mail", self.account)
                    self._recover(self._get_sync(LAST_SUCCESS_KEY))
                    return False
                raise
            records = page.get("history", []) or []
            self._count("history_records", len(records))
            events.extend(_events_from_history(records))
            latest = str(page.get("historyId") or latest)
            page_token = str(page.get("nextPageToken") or "")
            if not page_token:
                break
        else:
            logger.warning("Gmail history for {} is longer than {} pages; re-listing recent mail", self.account, _HISTORY_PAGES_MAX)
            self._recover(self._get_sync(LAST_SUCCESS_KEY))
            return False
        self._apply_events(events)
        self._set_sync(HISTORY_ID_KEY, latest)
        return True

    def _apply_events(self, events: Sequence[_MessageEvent]) -> None:
        if not events:
            return
        ids = {e.message_id for e in events}
        rows = self.store.gmail_label_rows(self.account, ids)
        stored_ids = self.store.existing_record_ids(SourceKind.GMAIL, self.account, ids - set(rows))
        stored = self.store.stored_gmail_labels(self.account, stored_ids)
        full_known = self._full_known_labels()
        working: dict[str, _Working] = {}
        for event in events:
            state = working.get(event.message_id)
            if state is None:
                state = _initial_working(self.account, event, rows, stored, full_known)
                working[event.message_id] = state
            _apply_event(state, event)
        self._count("added", sum(1 for w in working.values() if w.is_added))
        self._fill_from_gmail(working)
        self._write_working(working)

    def _fill_from_gmail(self, working: dict[str, _Working]) -> None:
        """Fetch what history alone cannot say: new messages in full, labels of messages stored
        before they had a label row. Gmail's answer is current, so it replaces what history said."""
        full_ids = [mid for mid, w in working.items() if w.needs_full and not w.is_deleted]
        label_ids = [mid for mid, w in working.items() if w.needs_labels and not w.is_deleted]
        at = self.clock()
        for start in range(0, len(full_ids), _WRITE_BATCH):
            fetched = self._fetch_many(full_ids[start : start + _WRITE_BATCH], "full")
            with self.store.transaction():
                for message_id, raw, error in fetched:
                    state = working[message_id]
                    if error is not None:
                        # Not stored: retried next run rather than recorded as half a row.
                        self._note_failed(message_id, error)
                        continue
                    if raw is None:
                        state.is_deleted = True
                        continue
                    row = self._store_full(raw, at)
                    _take_fetched(state, row)
                    self._count("fetched", 1)
        for message_id, raw, error in self._fetch_many(label_ids, "minimal"):
            state = working[message_id]
            if error is not None:
                if error != _DEADLINE:
                    log_ingest_problem(self.ingest_log, self.account, message_id, error)
                continue
            if raw is None:
                state.is_deleted = True
                continue
            _take_fetched(state, label_row_from_raw(self.account, raw))
            state.needs_labels = False

    def _write_working(self, working: dict[str, _Working]) -> None:
        at = self.clock()
        items = list(working.items())
        for start in range(0, len(items), _WRITE_BATCH):
            with self.store.transaction():
                for message_id, state in items[start : start + _WRITE_BATCH]:
                    if state.needs_full and not state.is_deleted:
                        # Fetch failed: no copy to describe yet; retried next run.
                        continue
                    change = _change_kind(state)
                    if change is None:
                        continue
                    self.store.save_gmail_label_row(_row_from_working(self.account, message_id, state), change, at)
                    self._count("label_changes", 0 if change == GmailChangeKind.ADDED else 1)

    # --- listing ---

    def _list_ids(self, params: Sequence[tuple[str, str]]) -> dict[str, str]:
        """Every id (to its thread id) a listing returns, all pages."""
        found: dict[str, str] = {}
        page_token = ""
        is_more = True
        while is_more:
            page_params = [*params, ("maxResults", str(_LIST_PAGE_SIZE))]
            if page_token:
                page_params.append(("pageToken", page_token))
            page = self._get("messages", page_params)
            for ref in page.get("messages", []) or []:
                found[str(ref["id"])] = str(ref.get("threadId") or ref["id"])
            page_token = str(page.get("nextPageToken") or "")
            is_more = bool(page_token)
        return found

    def _recover(self, last_success: str | None) -> None:
        """No usable history position: store recent mail that is missing, then re-baseline labels."""
        profile = self._get("profile", {})
        history_id = str(profile["historyId"])
        if last_success:
            since = datetime.fromisoformat(last_success) - _RECOVERY_OVERLAP
            listed = self._list_ids([("q", f"after:{int(since.timestamp())}"), ("includeSpamTrash", "false")])
            missing = sorted(set(listed) - self.store.existing_record_ids(SourceKind.GMAIL, self.account, set(listed)))
            self._backfill(missing)
        with self.store.transaction():
            self.store.set_sync_value(SourceKind.GMAIL, self.account, HISTORY_ID_KEY, history_id)
            # Labels may have changed unseen: list them again.
            self.store.set_sync_value(SourceKind.GMAIL, self.account, BASELINE_KEY, "{}")

    # --- baseline ---

    def baseline_state(self) -> str:
        baseline = self._baseline()
        if baseline.get("is_done"):
            return "done"
        if not baseline.get("is_inbox_done"):
            return "listing the inbox"
        return f"listing labels ({len(baseline.get('done', []))} of {len(baseline.get('plan', {}))})"

    def run_baseline(self) -> None:
        baseline = self._baseline()
        if baseline.get("is_done"):
            return
        if not baseline.get("is_inbox_done"):
            self.reconcile_inbox()
            baseline["is_inbox_done"] = True
            self._save_baseline(baseline)
            self._mark_full_known(_INBOX)
        if "plan" not in baseline:
            baseline["plan"] = self._plan_labels()
            baseline["done"] = []
            self._save_baseline(baseline)
        plan: dict[str, str] = baseline["plan"]
        for label_id in sorted(plan):
            if label_id in baseline["done"]:
                continue
            self._apply_label_listing(label_id, is_full=plan[label_id] == "full")
            baseline["done"].append(label_id)
            self._save_baseline(baseline)
            if plan[label_id] == "full":
                self._mark_full_known(label_id)
        if not baseline.get("is_drafts_checked"):
            self._check_stored_drafts()
            baseline["is_drafts_checked"] = True
            self._save_baseline(baseline)
        if not baseline.get("is_sent_checked"):
            listed = self._list_ids([("labelIds", "SENT"), ("includeSpamTrash", "false")])
            missing = sorted(set(listed) - self.store.existing_record_ids(SourceKind.GMAIL, self.account, set(listed)))
            logger.info("Gmail {}: {} sent messages listed, {} missing from the archive", self.account, len(listed), len(missing))
            self._backfill(missing)
            baseline["is_sent_checked"] = True
            self._save_baseline(baseline)
        if not baseline.get("is_bcc_indexed"):
            self._index_sent_bcc(baseline)
            baseline["is_bcc_indexed"] = True
            self._save_baseline(baseline)
        baseline["is_done"] = True
        self._save_baseline(baseline)
        self._set_sync(RECONCILED_AT_KEY, self.clock().isoformat())

    def verify_recent(self) -> None:
        """The last, slowest part of the baseline (see _verify_recent_unrowed); runs once the rest
        is done and the inbox copies are filled, with whatever time a run has left."""
        baseline = self._baseline()
        if not baseline.get("is_done") or baseline.get("is_recent_verified"):
            return
        self._verify_recent_unrowed(baseline)
        baseline["is_recent_verified"] = True
        self._save_baseline(baseline)

    def _check_stored_drafts(self) -> None:
        """Drafts the CRM stored before the sync began may since have been sent or thrown away,
        which it never recorded. Each one not among Gmail's drafts now is asked about once: gone
        ones are marked deleted (so a thread never shows a draft that no longer exists)."""
        drafts = self.store.stored_draft_ids(self.account)
        unknown = sorted(set(drafts) - set(self.store.gmail_label_rows(self.account, set(drafts))))
        at = self.clock()
        stored = self.store.stored_gmail_labels(self.account, set(unknown))
        fetched = self._fetch_many(unknown, "minimal")
        with self.store.transaction():
            for message_id, raw, error in fetched:
                if error is not None:
                    continue
                if raw is None:
                    thread_id, internal_ms, _labels = stored[message_id]
                    gone = GmailLabelRow(
                        account=self.account, message_id=GmailMessageId(message_id), thread_id=GmailThreadId(thread_id),
                        internal_ms=internal_ms, label_ids=(), is_deleted=True,
                    )
                    self.store.save_gmail_label_row(gone, GmailChangeKind.DELETED, at)
                else:
                    self.store.save_gmail_label_row(label_row_from_raw(self.account, raw), GmailChangeKind.LABELS, at)
        logger.info("Gmail {}: {} stored drafts checked against Gmail", self.account, len(unknown))

    def _verify_recent_unrowed(self, baseline: dict[str, Any]) -> None:
        """Recent mail stored before the sync began may since have been deleted in Gmail (a
        Superhuman reminder, a sent draft), which nothing recorded; it would still show in its
        thread. Each recent message with no label row is asked about once, newest first, a slice
        per run: gone ones are marked deleted, the rest get their current labels. Done after the
        rest of the baseline, so readers never wait for it."""
        since = self.clock() - RECENT_VERIFY_WINDOW
        pending = self.store.unrowed_message_ids_since(self.account, since)
        logger.info("Gmail {}: {} recent stored messages to check against Gmail", self.account, len(pending))
        for start in range(0, len(pending), _WRITE_BATCH):
            chunk = pending[start : start + _WRITE_BATCH]
            stored = self.store.stored_gmail_labels(self.account, set(chunk))
            fetched = self._fetch_many(chunk, "minimal")
            at = self.clock()
            with self.store.transaction():
                for message_id, raw, error in fetched:
                    if error is not None:
                        continue
                    if raw is None:
                        thread_id, internal_ms, _labels = stored[message_id]
                        gone = GmailLabelRow(
                            account=self.account, message_id=GmailMessageId(message_id), thread_id=GmailThreadId(thread_id),
                            internal_ms=internal_ms, label_ids=(), is_deleted=True,
                        )
                        self.store.save_gmail_label_row(gone, GmailChangeKind.DELETED, at)
                        self._count("label_changes", 1)
                    else:
                        self.store.save_gmail_label_row(label_row_from_raw(self.account, raw), None, at)
            self._check_time()

    def _index_sent_bcc(self, baseline: dict[str, Any]) -> None:
        """Record who each stored sent message was Bcc'd to (Gmail's "sent to X" counts Bcc; the
        archive's participants do not). Reads only the archive; resumes where it stopped."""
        done_through = str(baseline.get("bcc_indexed_through", ""))
        sent_ids = [i for i in self.store.sent_message_ids(self.account) if i > done_through]
        for start in range(0, len(sent_ids), _WRITE_BATCH * 5):
            self._check_time()
            chunk = sent_ids[start : start + _WRITE_BATCH * 5]
            raws = self.store.raw_records(SourceKind.GMAIL, self.account, set(chunk))
            with self.store.transaction():
                for message_id, raw in raws.items():
                    self.store.save_sent_bcc(self.account, message_id, bcc_addresses(raw))
            baseline["bcc_indexed_through"] = chunk[-1]
            self._save_baseline(baseline)

    def _mark_full_known(self, label_id: str) -> None:
        known = self._full_known_labels()
        known.add(label_id)
        self._set_sync(FULL_KNOWN_LABELS_KEY, json.dumps(sorted(known)))

    def _plan_labels(self) -> dict[str, str]:
        """Each label to list: in full when small enough, otherwise only within the inbox."""
        plan: dict[str, str] = {}
        for label in self._get("labels", {}).get("labels", []) or []:
            label_id = str(label["id"])
            if label_id in _NOT_BASELINED:
                continue
            total = int(self._get(f"labels/{label_id}", {}).get("messagesTotal", 0) or 0)
            plan[label_id] = "full" if total <= self.full_listing_max else "inbox"
        return plan

    def _apply_label_listing(self, label_id: str, is_full: bool) -> None:
        if is_full:
            listed = self._list_ids([("labelIds", label_id), ("includeSpamTrash", "true")])
        else:
            listed = self._list_ids([("labelIds", _INBOX), ("labelIds", label_id), ("includeSpamTrash", "false")])
        holders = self.store.gmail_label_row_ids(self.account, label_id, is_inbox_only=not is_full)
        self._set_label_membership(label_id, listed, holders)

    def _set_label_membership(self, label_id: str, listed: dict[str, str], holders: set[str]) -> None:
        """Make exactly the listed messages carry the label (among those the listing covers)."""
        to_add = set(listed) - holders
        to_remove = holders - set(listed)
        touched = to_add | to_remove
        if not touched:
            return
        rows = self.store.gmail_label_rows(self.account, touched)
        stored = self.store.stored_gmail_labels(self.account, touched - set(rows))
        full_known = self._full_known_labels()
        at = self.clock()
        items = sorted(touched)
        changed = 0
        for start in range(0, len(items), _WRITE_BATCH):
            with self.store.transaction():
                for message_id in items[start : start + _WRITE_BATCH]:
                    row = rows.get(message_id) or _row_without_label_state(
                        self.account, message_id, listed.get(message_id, message_id), stored.get(message_id), full_known
                    )
                    labels = set(row.label_ids)
                    if message_id in to_add:
                        labels.add(label_id)
                    else:
                        labels.discard(label_id)
                    updated = row.model_copy(update={"label_ids": tuple(sorted(labels))})
                    # A first row only records what readers already inferred unless the label moved.
                    is_changed = updated.label_ids != row.label_ids
                    self.store.save_gmail_label_row(updated, GmailChangeKind.LABELS if is_changed else None, at)
                    changed += 1 if is_changed else 0
        self._count("label_changes", changed)
        logger.info("Gmail {} label {}: {} listed, {} added, {} removed", self.account, label_id, len(listed), len(to_add), len(to_remove))

    # --- the inbox ---

    def reconcile_inbox(self) -> int:
        """Make the inbox rows match Gmail's inbox listing; returns how many differed."""
        listed = self._list_ids([("labelIds", _INBOX), ("includeSpamTrash", "false")])
        holders = self.store.gmail_label_row_ids(self.account, _INBOX, is_inbox_only=True)
        mismatches = len(set(listed) ^ holders)
        if self._baseline().get("is_inbox_done") and mismatches:
            logger.warning("Gmail {}: the inbox check found {} messages history had not reported", self.account, mismatches)
        self._set_label_membership(_INBOX, listed, holders)
        self._count("inbox_mismatches", mismatches)
        return mismatches

    def reconcile_if_due(self) -> None:
        last = self._get_sync(RECONCILED_AT_KEY)
        if last and self.clock() - datetime.fromisoformat(last) < RECONCILE_INTERVAL:
            return
        self.reconcile_inbox()
        self._set_sync(RECONCILED_AT_KEY, self.clock().isoformat())

    def backfill_inbox(self) -> None:
        """Fetch inbox messages the archive has no copy of, newest first, until the deadline."""
        missing = self.store.gmail_inbox_ids_missing_raw(self.account, _WRITE_BATCH)
        while missing:
            self._backfill(missing)
            following = self.store.gmail_inbox_ids_missing_raw(self.account, _WRITE_BATCH)
            # Ids that failed again come back; stop rather than loop on them this run.
            missing = [] if set(following) >= set(missing) else following

    def _backfill(self, message_ids: Sequence[str]) -> None:
        for start in range(0, len(message_ids), _WRITE_BATCH):
            fetched = self._fetch_many(message_ids[start : start + _WRITE_BATCH], "full")
            at = self.clock()
            existing = self.store.gmail_label_rows(self.account, {mid for mid, _raw, _error in fetched})
            with self.store.transaction():
                for message_id, raw, error in fetched:
                    if error is not None:
                        self._note_failed(message_id, error)
                        continue
                    if raw is None:
                        gone = existing.get(message_id)
                        if gone is not None:
                            self.store.save_gmail_label_row(gone.model_copy(update={"is_deleted": True}), GmailChangeKind.DELETED, at)
                        continue
                    row = self._store_full(raw, at)
                    self.store.save_gmail_label_row(row, GmailChangeKind.ADDED, at)
                    self._count("backfilled", 1)
            self._check_time()

    def summary(self, is_caught_up: bool) -> GmailSyncSummary:
        return GmailSyncSummary(
            account=self.account,
            history_records=self._counts.get("history_records", 0),
            added_count=self._counts.get("added", 0),
            fetched_count=self._counts.get("fetched", 0),
            label_change_count=self._counts.get("label_changes", 0),
            backfilled_count=self._counts.get("backfilled", 0),
            inbox_mismatch_count=self._counts.get("inbox_mismatches", 0),
            baseline_state=self.baseline_state(),
            is_caught_up=is_caught_up,
            request_count=self._requests,
        )


def _events_from_history(records: Sequence[dict[str, Any]]) -> list[_MessageEvent]:
    """History records flattened to per-message events, in Gmail's order."""
    events: list[_MessageEvent] = []
    for record in records:
        for key, kind in (("messagesAdded", "added"), ("messagesDeleted", "deleted"), ("labelsAdded", "plus"), ("labelsRemoved", "minus")):
            for item in record.get(key, []) or []:
                message = item.get("message") or {}
                message_id = str(message.get("id", ""))
                if not message_id:
                    continue
                labels = item.get("labelIds") if kind in ("plus", "minus") else message.get("labelIds")
                events.append(
                    _MessageEvent(
                        kind=kind,
                        message_id=message_id,
                        thread_id=str(message.get("threadId") or message_id),
                        label_ids=tuple(str(label) for label in labels or []),
                    )
                )
    return events


def _row_without_label_state(
    account: AccountEmail,
    message_id: str,
    thread_id: str,
    stored: tuple[str, int, tuple[str, ...]] | None,
    full_known: set[str],
) -> GmailLabelRow:
    """A first row for a message: its labels as fetched, minus every label known in full (it has
    none of those, or it would have a row already)."""
    if stored is None:
        return GmailLabelRow(
            account=account, message_id=GmailMessageId(message_id), thread_id=GmailThreadId(thread_id),
            internal_ms=0, label_ids=(), is_deleted=False,
        )
    stored_thread, internal_ms, labels = stored
    return GmailLabelRow(
        account=account,
        message_id=GmailMessageId(message_id),
        thread_id=GmailThreadId(stored_thread),
        internal_ms=internal_ms,
        label_ids=tuple(sorted(set(labels) - full_known)),
        is_deleted=False,
    )


def _initial_working(
    account: AccountEmail,
    event: _MessageEvent,
    rows: dict[str, GmailLabelRow],
    stored: dict[str, tuple[str, int, tuple[str, ...]]],
    full_known: set[str],
) -> _Working:
    row = rows.get(event.message_id)
    if row is not None:
        return _Working(
            thread_id=str(row.thread_id), internal_ms=row.internal_ms, labels=set(row.label_ids),
            is_deleted=row.is_deleted, is_added=False, needs_full=False, needs_labels=False,
            is_stored=row.internal_ms > 0, before=(row.label_ids, row.is_deleted),
        )
    stored_labels = stored.get(event.message_id)
    if stored_labels is not None:
        base = _row_without_label_state(account, event.message_id, event.thread_id, stored_labels, full_known)
        return _Working(
            thread_id=str(base.thread_id), internal_ms=base.internal_ms, labels=set(base.label_ids),
            is_deleted=False, is_added=False, needs_full=False,
            # A stored message Gmail reports as new was stored by another pull: ask its labels.
            needs_labels=event.kind == "added",
            is_stored=True,
            before=None,
        )
    return _Working(
        thread_id=event.thread_id, internal_ms=0, labels=set(), is_deleted=False, is_added=False,
        needs_full=event.kind == "added", needs_labels=False, is_stored=False, before=None,
    )


def _apply_event(state: _Working, event: _MessageEvent) -> None:
    if event.kind == "added":
        state.is_added = True
        state.labels = set(event.label_ids) or state.labels
    elif event.kind == "deleted":
        state.is_deleted = True
    elif event.kind == "plus":
        state.labels |= set(event.label_ids)
        # An old message moved into the inbox has to be in the archive for the reviews to read it.
        if _INBOX in event.label_ids and not state.is_stored:
            state.needs_full = True
    else:
        state.labels -= set(event.label_ids)


def _take_fetched(state: _Working, row: GmailLabelRow) -> None:
    state.labels = set(row.label_ids)
    state.thread_id = str(row.thread_id)
    state.internal_ms = row.internal_ms
    state.needs_full = False
    state.is_stored = True


def _change_kind(state: _Working) -> GmailChangeKind | None:
    if state.is_deleted:
        return None if state.before is not None and state.before[1] else GmailChangeKind.DELETED
    if state.is_added:
        return GmailChangeKind.ADDED
    if state.before is None or state.before[0] != tuple(sorted(state.labels)):
        return GmailChangeKind.LABELS
    return None


def _row_from_working(account: AccountEmail, message_id: str, state: _Working) -> GmailLabelRow:
    labels = set() if state.is_deleted else state.labels
    return GmailLabelRow(
        account=account,
        message_id=GmailMessageId(message_id),
        thread_id=GmailThreadId(state.thread_id),
        internal_ms=state.internal_ms,
        label_ids=tuple(sorted(labels)),
        is_deleted=state.is_deleted,
    )


def sync_gmail_accounts(
    gateway: GoogleApiGatewayInterface,
    store: ArchiveStore,
    accounts: Sequence[AccountEmail],
    deadline: datetime,
    ingest_log: Path,
    clock: Callable[[], datetime] = _now,
    full_listing_max: int = FULL_LISTING_MAX,
    is_history_only: bool = False,
) -> list[GmailSyncSummary]:
    """One run: history for every account first (that is what keeps the store fresh), then, with
    the time left, each account's retries, baseline, gap filling and inbox check (skipped for a
    history-only catch-up). One account that cannot be reached does not stop the other."""
    syncs = [
        AccountSync(
            gateway=gateway, store=store, account=account, deadline=deadline, ingest_log=ingest_log,
            clock=clock, full_listing_max=full_listing_max,
        )
        for account in accounts
    ]
    caught_up: dict[str, bool] = {}
    for sync in syncs:
        try:
            caught_up[str(sync.account)] = sync.apply_history()
        except OutOfTimeError:
            logger.warning("Gmail {}: history not finished before the deadline", sync.account)
            caught_up[str(sync.account)] = False
        except (GatewayUnreachableError, CredentialsExpiredError, GatewayRateLimitedError) as e:
            logger.error("Gmail {}: cannot reach the mailbox: {}", sync.account, e)
            caught_up[str(sync.account)] = False
        if caught_up[str(sync.account)]:
            sync._set_sync(LAST_SUCCESS_KEY, clock().isoformat())
    for sync in syncs:
        if is_history_only or not caught_up[str(sync.account)]:
            sync.save_retries()
            continue
        try:
            sync.retry_failed()
            sync.run_baseline()
            sync.backfill_inbox()
            sync.reconcile_if_due()
            sync.verify_recent()
        except OutOfTimeError:
            logger.info("Gmail {}: deadline reached; the rest continues next run ({})", sync.account, sync.baseline_state())
        except (GatewayUnreachableError, CredentialsExpiredError, GatewayRateLimitedError) as e:
            logger.error("Gmail {}: lost the mailbox part way: {}", sync.account, e)
        sync.save_retries()
    with store.transaction():
        store.prune_gmail_changes(clock() - CHANGE_FEED_RETENTION)
    return [sync.summary(caught_up[str(sync.account)]) for sync in syncs]
