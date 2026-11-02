import json
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from people_archive.data_types import GmailChangeKind, PullSummary
from people_archive.errors import GatewayRateLimitedError, UpstreamApiError
from people_archive.gmail_parse import (
    bcc_addresses,
    label_row_from_raw,
    parse_gmail_message,
)
from people_archive.interfaces import GoogleApiGatewayInterface
from people_archive.primitives import AccountEmail, SourceKind
from people_archive.rate_limit import DEFAULT_START_RATE_PER_SECOND, AdaptiveRateLimiter
from people_archive.store import ArchiveStore

GMAIL_BASE: Final[str] = "https://gmail.googleapis.com/gmail/v1/users/me"
_LIST_PAGE_SIZE: Final[int] = 500
# Concurrency only bounds in-flight calls; the adaptive limiter sets the actual pace,
# since the per-minute quota is shared with other jobs on the same account.
DEFAULT_FETCH_WORKERS: Final[int] = 6
_HISTORY_ID_KEY: Final[str] = "history_id"
_FULL_PULL_DONE_KEY: Final[str] = "full_pull_done"
# The full walk only covers mail outside Gmail's bulk categories (promotions, updates, forums,
# categories are more than half of each mailbox. The pass skips ids already stored, so it can
# be interrupted and resumed.
_FULL_WALK_QUERIES: Final[tuple[str, ...]] = (
    "-category:promotions -category:updates -category:forums -category:social",
)


@retry(
    retry=retry_if_exception_type(GatewayRateLimitedError),
    stop=stop_after_attempt(10),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def get_with_backoff(
    gateway: GoogleApiGatewayInterface,
    limiter: AdaptiveRateLimiter,
    account: AccountEmail,
    url: str,
    params: Mapping[str, str] | Sequence[tuple[str, str]],
) -> dict[str, Any]:
    limiter.wait_for_slot()
    try:
        body = gateway.get_json(account, url, params)
    except GatewayRateLimitedError:
        limiter.record_rate_limited()
        raise
    limiter.record_success()
    return body


def _fetch_message(gateway: GoogleApiGatewayInterface, limiter: AdaptiveRateLimiter, account: AccountEmail, message_id: str) -> dict[str, Any] | None:
    """The full message, or None when it is gone (deleted between list and get)."""
    try:
        return get_with_backoff(gateway, limiter, account, f"{GMAIL_BASE}/messages/{message_id}", {"format": "full"})
    except UpstreamApiError as e:
        if "404" in str(e):
            return None
        raise


def log_ingest_problem(ingest_log: Path, account: AccountEmail, message_id: str, reason: str) -> None:
    ingest_log.parent.mkdir(parents=True, exist_ok=True)
    with ingest_log.open("a") as handle:
        handle.write(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(), "source": SourceKind.GMAIL.value,
            "account": str(account), "message_id": message_id, "reason": reason[:300],
        }) + "\n")


def _store_batch(
    gateway: GoogleApiGatewayInterface,
    limiter: AdaptiveRateLimiter,
    store: ArchiveStore,
    account: AccountEmail,
    message_ids: Sequence[str],
    workers: int,
    ingest_log: Path,
) -> tuple[int, int]:
    """Fetch the given ids concurrently and write them in one transaction; returns (stored, failed)."""
    stored = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fetched = list(pool.map(lambda mid: _fetch_message_or_error(gateway, limiter, account, mid), message_ids))
    fetched_at = datetime.now(timezone.utc)
    with store.transaction():
        for message_id, raw, error in fetched:
            if error is not None:
                failed += 1
                log_ingest_problem(ingest_log, account, message_id, error)
                continue
            if raw is None:
                continue
            store.upsert_raw_record(SourceKind.GMAIL, account, message_id, raw, fetched_at)
            store.upsert_message(parse_gmail_message(account, raw))
            # Every stored message gets its label row, which the Gmail sync keeps current from here.
            store.save_gmail_label_row(label_row_from_raw(account, raw), GmailChangeKind.ADDED, fetched_at)
            if "SENT" in (raw.get("labelIds") or []):
                store.save_sent_bcc(account, message_id, bcc_addresses(raw))
            stored += 1
    return stored, failed


def _fetch_message_or_error(
    gateway: GoogleApiGatewayInterface, limiter: AdaptiveRateLimiter, account: AccountEmail, message_id: str,
) -> tuple[str, dict[str, Any] | None, str | None]:
    try:
        return message_id, _fetch_message(gateway, limiter, account, message_id), None
    except (GatewayRateLimitedError, UpstreamApiError) as e:
        return message_id, None, str(e)


def _current_history_id(gateway: GoogleApiGatewayInterface, limiter: AdaptiveRateLimiter, account: AccountEmail) -> str:
    profile = get_with_backoff(gateway, limiter, account, f"{GMAIL_BASE}/profile", {})
    return str(profile["historyId"])


def _walk_listing(
    gateway: GoogleApiGatewayInterface,
    limiter: AdaptiveRateLimiter,
    store: ArchiveStore,
    account: AccountEmail,
    query: str,
    workers: int,
    ingest_log: Path,
) -> tuple[int, int, int]:
    """Page through one listing query newest-first, storing what is missing; returns (stored, skipped, failed)."""
    fetched_total = 0
    skipped_total = 0
    failed_total = 0
    page_token: str | None = ""
    page_index = 0
    while page_token is not None:
        params = {"maxResults": str(_LIST_PAGE_SIZE), "includeSpamTrash": "false"}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        page = get_with_backoff(gateway, limiter, account, f"{GMAIL_BASE}/messages", params)
        page_ids = [str(m["id"]) for m in page.get("messages", []) or []]
        already = store.existing_record_ids(SourceKind.GMAIL, account, set(page_ids))
        missing = [mid for mid in page_ids if mid not in already]
        stored, failed = _store_batch(gateway, limiter, store, account, missing, workers, ingest_log) if missing else (0, 0)
        fetched_total += stored
        skipped_total += len(already)
        failed_total += failed
        page_index += 1
        logger.info(
            "Gmail {} [{}] page {}: {} listed, {} new stored, {} already present, {} failed (pass total {}, rate {:.1f}/s)",
            account, query or "all", page_index, len(page_ids), stored, len(already), failed, fetched_total, limiter.rate_per_second,
        )
        page_token = page.get("nextPageToken") or None
    return fetched_total, skipped_total, failed_total


def pull_gmail_full(
    gateway: GoogleApiGatewayInterface,
    store: ArchiveStore,
    account: AccountEmail,
    workers: int,
    ingest_log: Path,
) -> PullSummary:
    """Walk the whole mailbox newest-first, fetching every message not already stored.

    Resumable: a killed run picks up where the ids in the store end. The history id is
    recorded *before* listing so the incremental pull afterwards cannot miss anything.
    """
    limiter = AdaptiveRateLimiter(rate_per_second=DEFAULT_START_RATE_PER_SECOND)
    history_id_at_start = _current_history_id(gateway, limiter, account)
    fetched_total = 0
    skipped_total = 0
    failed_total = 0
    for query in _FULL_WALK_QUERIES:
        fetched, skipped, failed = _walk_listing(gateway, limiter, store, account, query, workers, ingest_log)
        fetched_total += fetched
        skipped_total += skipped
        failed_total += failed
    with store.transaction():
        store.set_sync_value(SourceKind.GMAIL, account, _HISTORY_ID_KEY, history_id_at_start)
        store.set_sync_value(SourceKind.GMAIL, account, _FULL_PULL_DONE_KEY, datetime.now(timezone.utc).isoformat())
    return PullSummary(source=SourceKind.GMAIL, account=account, fetched_count=fetched_total, skipped_count=skipped_total, failed_count=failed_total)


def pull_gmail_incremental(
    gateway: GoogleApiGatewayInterface,
    store: ArchiveStore,
    account: AccountEmail,
    workers: int,
    ingest_log: Path,
) -> PullSummary:
    """Fetch only what changed since the recorded history id; falls back to a full walk when
    there is no history id yet or Gmail has expired it."""
    start_history_id = store.get_sync_value(SourceKind.GMAIL, account, _HISTORY_ID_KEY)
    if start_history_id is None:
        return pull_gmail_full(gateway, store, account, workers, ingest_log)
    limiter = AdaptiveRateLimiter(rate_per_second=DEFAULT_START_RATE_PER_SECOND)
    new_history_id = _current_history_id(gateway, limiter, account)
    added_ids: list[str] = []
    page_token: str | None = ""
    while page_token is not None:
        params = {"startHistoryId": start_history_id, "historyTypes": "messageAdded", "maxResults": "500"}
        if page_token:
            params["pageToken"] = page_token
        try:
            page = get_with_backoff(gateway, limiter, account, f"{GMAIL_BASE}/history", params)
        except UpstreamApiError as e:
            if "404" in str(e):
                logger.warning("Gmail history for {} expired; walking the mailbox again", account)
                return pull_gmail_full(gateway, store, account, workers, ingest_log)
            raise
        for record in page.get("history", []) or []:
            for added in record.get("messagesAdded", []) or []:
                added_ids.append(str(added["message"]["id"]))
        page_token = page.get("nextPageToken") or None
    unique_ids = list(dict.fromkeys(added_ids))
    already = store.existing_record_ids(SourceKind.GMAIL, account, set(unique_ids))
    missing = [mid for mid in unique_ids if mid not in already]
    stored, failed = _store_batch(gateway, limiter, store, account, missing, workers, ingest_log) if missing else (0, 0)
    with store.transaction():
        store.set_sync_value(SourceKind.GMAIL, account, _HISTORY_ID_KEY, new_history_id)
    logger.info("Gmail {} incremental: {} new stored, {} already present, {} failed", account, stored, len(already), failed)
    return PullSummary(source=SourceKind.GMAIL, account=account, fetched_count=stored, skipped_count=len(already), failed_count=failed)


def is_full_pull_done(store: ArchiveStore, account: AccountEmail) -> bool:
    return store.get_sync_value(SourceKind.GMAIL, account, _FULL_PULL_DONE_KEY) is not None
