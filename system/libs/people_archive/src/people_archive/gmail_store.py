"""Read the user's mail from the archive instead of calling Gmail.

The Gmail sync (gmail_sync.py, every few minutes) keeps the archive's copy of both mailboxes and
every message's labels current. This module is the read side for the email reviews' scripts and
Today: it answers the questions they used to ask Gmail, in the shapes Gmail answered them
(``messages.get``, ``threads.get``, ``messages.list``), so their code that reads those shapes stays
as it was.

It uses only the standard library and opens the archive read-only, so any script can import it
cheaply and it can never change the store. Writes (send, archive, label, drafts) still go to Gmail;
the next sync picks up their result.

Before reading, a caller checks ``readiness(account)``: when the sync has not succeeded recently
(STALE_AFTER_SECONDS) the caller reads Gmail directly instead and records that with
``log_fallback``.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import zlib
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final[Path] = Path("/home/user/workspace")
DEFAULT_ARCHIVE_PATH: Final[Path] = REPO_ROOT / "data/.apps/crm/archive.sqlite"
# Points readers at another archive (a test's, or an isolated instance's).
ARCHIVE_PATH_ENV: Final[str] = "GMAIL_STORE_PATH"
STALE_AFTER_SECONDS: Final[int] = 15 * 60
# Set to 0 to stop readers asking the sync to catch up first (a test reading a fixed copy).
CATCH_UP_ENV: Final[str] = "GMAIL_STORE_CATCH_UP"
_SOURCE: Final[str] = "GMAIL"
_LAST_SUCCESS_KEY: Final[str] = "sync_last_success"
_FULL_KNOWN_KEY: Final[str] = "labels_full_known"
_BASELINE_KEY: Final[str] = "labels_baseline"
# Gmail leaves trash and spam out of searches and listings unless asked.
_HIDDEN_LABELS: Final[frozenset[str]] = frozenset({"TRASH", "SPAM"})
_IN_CHUNK: Final[int] = 500


def archive_path() -> Path:
    return Path(os.environ.get(ARCHIVE_PATH_ENV) or DEFAULT_ARCHIVE_PATH)


def fallback_log_path() -> Path:
    return archive_path().parent / "logs" / "gmail-store-fallbacks.jsonl"


def log_fallback(consumer: str, account: str, reason: str) -> None:
    """Record that a consumer read Gmail directly because the store could not answer."""
    path = fallback_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(
                json.dumps(
                    {"at": datetime.now(timezone.utc).isoformat(), "consumer": consumer, "account": account, "reason": reason[:300]}
                )
                + "\n"
            )
    except OSError:
        return


def _sync_command() -> list[str]:
    found = shutil.which("people-archive")
    if found:
        return [found]
    installed = REPO_ROOT / ".venv/bin/people-archive"
    return [str(installed)] if installed.exists() else ["uv", "run", "--no-sync", "people-archive"]


# A catch-up this recent already covers the caller (one refresh runs two passes back to back).
CATCH_UP_FRESH_SECONDS: Final[int] = 60


def catch_up(account: str, wait_seconds: int = 60, is_forced: bool = False) -> bool:
    """Have the Gmail sync apply what changed in this mailbox right now (a couple of Gmail calls),
    so a refresh just after an archive or a reply sees it. Waits for a sync already running.
    Skipped when the store caught up within the last minute, unless forced (after the caller's
    own write to Gmail). False when it could not run; the store is then as fresh as the last
    scheduled sync."""
    if os.environ.get(CATCH_UP_ENV, "1") == "0":
        return False
    if not is_forced:
        store = open_store()
        if store is not None:
            age = store.readiness(account)["age_seconds"]
            store.close()
            if age is not None and age < CATCH_UP_FRESH_SECONDS:
                return True
    command = [
        *_sync_command(), "sync-gmail", "--store", str(archive_path()), "--account", account,
        "--history-only", "--wait", str(wait_seconds), "--time-limit", "60",
    ]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=wait_seconds + 120, cwd=REPO_ROOT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


class StoreUnavailableError(OSError):
    """The archive could not be opened for reading."""


def _sort_key(message_id: str) -> str:
    return message_id.rjust(20, "0")


class GmailStore:
    """A read-only view of the synced mailboxes."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or archive_path()
        if not self.path.exists():
            raise StoreUnavailableError(f"No archive at {self.path}")
        try:
            self._db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.execute("SELECT 1 FROM gmail_label_state LIMIT 1").fetchall()
        except sqlite3.Error as e:
            raise StoreUnavailableError(f"Cannot read the archive at {self.path}: {e}") from e
        self._full_known: dict[str, frozenset[str]] = {}

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "GmailStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- freshness ---

    def _sync_value(self, account: str, key: str) -> str | None:
        row = self._db.execute(
            "SELECT value FROM sync_state WHERE source=? AND account=? AND key=?", (_SOURCE, account, key)
        ).fetchone()
        return None if row is None else str(row[0])

    def last_synced(self, account: str) -> datetime | None:
        value = self._sync_value(account, _LAST_SUCCESS_KEY)
        return datetime.fromisoformat(value) if value else None

    def readiness(self, account: str, now: datetime | None = None) -> dict[str, Any]:
        """How far the store can be trusted for this account right now.

        ``is_fresh``: the sync caught up within STALE_AFTER_SECONDS. ``is_inbox_ready``: the inbox
        has been listed, so which messages are in it is exact. ``is_labels_ready``: every label on
        inbox messages, and the labels listed in full, are exact too."""
        last = self.last_synced(account)
        moment = now or datetime.now(timezone.utc)
        age = None if last is None else (moment - last).total_seconds()
        baseline = json.loads(self._sync_value(account, _BASELINE_KEY) or "{}")
        return {
            "account": account,
            "last_synced": last.isoformat() if last else None,
            "age_seconds": age,
            "is_fresh": age is not None and age <= STALE_AFTER_SECONDS,
            "is_inbox_ready": bool(baseline.get("is_inbox_done")),
            "is_labels_ready": bool(baseline.get("is_done")),
        }

    def unusable_reason(self, account: str, needs_labels: bool) -> str | None:
        """Why the store cannot answer for this account now, or None when it can."""
        state = self.readiness(account)
        if not state["is_fresh"]:
            age = state["age_seconds"]
            return "never synced" if age is None else f"last synced {int(age // 60)} min ago"
        if not state["is_inbox_ready"]:
            return "inbox not listed yet"
        if needs_labels and not state["is_labels_ready"]:
            return "labels not listed yet"
        return None

    # --- labels ---

    def _full_known_labels(self, account: str) -> frozenset[str]:
        if account not in self._full_known:
            self._full_known[account] = frozenset(json.loads(self._sync_value(account, _FULL_KNOWN_KEY) or "[]"))
        return self._full_known[account]

    def _label_rows(self, account: str, message_ids: Sequence[str]) -> dict[str, tuple[str, list[str], bool]]:
        found: dict[str, tuple[str, list[str], bool]] = {}
        for start in range(0, len(message_ids), _IN_CHUNK):
            chunk = list(message_ids[start : start + _IN_CHUNK])
            marks = ",".join("?" for _ in chunk)
            for message_id, thread_id, labels_json, is_deleted in self._db.execute(
                f"SELECT message_id, thread_id, label_ids_json, is_deleted FROM gmail_label_state WHERE account=? AND message_id IN ({marks})",
                (account, *chunk),
            ):
                found[str(message_id)] = (str(thread_id), list(json.loads(labels_json)), bool(is_deleted))
        return found

    def _raws(self, account: str, message_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(message_ids), _IN_CHUNK):
            chunk = list(message_ids[start : start + _IN_CHUNK])
            marks = ",".join("?" for _ in chunk)
            for record_id, blob in self._db.execute(
                f"SELECT record_id, raw_zlib FROM raw_records WHERE source=? AND account=? AND record_id IN ({marks})",
                (_SOURCE, account, *chunk),
            ):
                found[str(record_id)] = json.loads(zlib.decompress(blob))
        return found

    def _current_labels(self, account: str, raw: dict[str, Any], row: tuple[str, list[str], bool] | None) -> list[str]:
        if row is not None:
            return list(row[1])
        # No label row: stored before the sync and in none of the labels it lists in full.
        known = self._full_known_labels(account)
        return [label for label in raw.get("labelIds", []) or [] if label not in known]

    def messages(self, account: str, message_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Each stored message as ``messages.get?format=full`` returns it, with its labels as they
        are now. Messages the archive holds no copy of, or Gmail deleted, are left out."""
        ids = list(dict.fromkeys(message_ids))
        rows = self._label_rows(account, ids)
        out: dict[str, dict[str, Any]] = {}
        for message_id, raw in self._raws(account, ids).items():
            row = rows.get(message_id)
            if row is not None and row[2]:
                continue
            out[message_id] = dict(raw, labelIds=self._current_labels(account, raw, row))
        return out

    def message(self, account: str, message_id: str) -> dict[str, Any] | None:
        return self.messages(account, [message_id]).get(message_id)

    def metadata(self, account: str, message_ids: Sequence[str], header_names: Iterable[str]) -> dict[str, dict[str, Any]]:
        """``messages.get?format=metadata&metadataHeaders=...``: the named headers only, as written."""
        wanted = {name.lower() for name in header_names}
        out: dict[str, dict[str, Any]] = {}
        for message_id, raw in self.messages(account, message_ids).items():
            out[message_id] = _metadata_shape(raw, wanted)
        return out

    # --- the inbox ---

    def inbox_ids(self, account: str) -> list[str]:
        """Every message in the inbox now, in the order ``messages.list?labelIds=INBOX`` gives."""
        return [
            str(row[0])
            for row in self._db.execute(
                "SELECT message_id FROM gmail_label_state WHERE account=? AND is_inbox=1 ORDER BY sort_key DESC", (account,)
            )
        ]

    def inbox_listing(self, account: str) -> list[dict[str, str]]:
        """The inbox as ``messages.list`` entries ({id, threadId}), newest first."""
        return [
            {"id": str(row[0]), "threadId": str(row[1])}
            for row in self._db.execute(
                "SELECT message_id, thread_id FROM gmail_label_state WHERE account=? AND is_inbox=1 ORDER BY sort_key DESC",
                (account,),
            )
        ]

    def missing_copies(self, account: str, message_ids: Sequence[str]) -> list[str]:
        """Which of these the archive holds no copy of yet."""
        have: set[str] = set()
        ids = list(message_ids)
        for start in range(0, len(ids), _IN_CHUNK):
            chunk = ids[start : start + _IN_CHUNK]
            marks = ",".join("?" for _ in chunk)
            have |= {
                str(row[0])
                for row in self._db.execute(
                    f"SELECT record_id FROM raw_records WHERE source=? AND account=? AND record_id IN ({marks})",
                    (_SOURCE, account, *chunk),
                )
            }
        return [message_id for message_id in ids if message_id not in have]

    # --- threads ---

    def thread_message_ids(self, account: str, thread_id: str) -> list[str]:
        """The thread's messages the archive knows (Gmail's deleted ones left out), oldest first."""
        ids = {
            str(row[0])
            for row in self._db.execute("SELECT message_id FROM messages WHERE account=? AND thread_id=?", (account, thread_id))
        }
        rows = self._db.execute(
            "SELECT message_id, is_deleted FROM gmail_label_state WHERE account=? AND thread_id=?", (account, thread_id)
        ).fetchall()
        ids |= {str(message_id) for message_id, is_deleted in rows if not is_deleted}
        ids -= {str(message_id) for message_id, is_deleted in rows if is_deleted}
        return sorted(ids, key=_sort_key)

    def thread_labels(self, account: str, thread_id: str) -> list[dict[str, Any]]:
        """``threads.get?format=minimal``: each message's id and current labels, including messages
        the archive has labels for but no copy of (old bulk mail), oldest first."""
        ids = self.thread_message_ids(account, thread_id)
        rows = self._label_rows(account, ids)
        unrowed = [i for i in ids if i not in rows]
        raws = self._raws(account, unrowed) if unrowed else {}
        out = []
        for message_id in ids:
            row = rows.get(message_id)
            if row is None and message_id not in raws:
                continue
            labels = self._current_labels(account, raws.get(message_id, {}), row)
            out.append({"id": message_id, "threadId": thread_id, "labelIds": labels})
        return out

    def thread(self, account: str, thread_id: str) -> dict[str, Any] | None:
        """``threads.get?format=full``: every stored message of the thread, oldest first."""
        ids = self.thread_message_ids(account, thread_id)
        stored = self.messages(account, ids)
        messages = sorted(stored.values(), key=lambda m: (int(m.get("internalDate", "0") or 0), _sort_key(str(m["id"]))))
        if not messages:
            return None
        return {"id": thread_id, "messages": messages}

    def thread_metadata(self, account: str, thread_id: str, header_names: Iterable[str]) -> dict[str, Any] | None:
        """``threads.get?format=metadata&metadataHeaders=...``."""
        thread = self.thread(account, thread_id)
        if thread is None:
            return None
        wanted = {name.lower() for name in header_names}
        return {"id": thread_id, "messages": [_metadata_shape(m, wanted) for m in thread["messages"]]}

    # --- what the user sent ---

    def _visible(self, account: str, message_ids: Sequence[str]) -> set[str]:
        """The ids Gmail's search would show: not deleted, not in trash or spam."""
        rows = self._label_rows(account, message_ids)
        visible = set()
        for message_id in message_ids:
            row = rows.get(message_id)
            if row is None or (not row[2] and not (set(row[1]) & _HIDDEN_LABELS)):
                visible.add(message_id)
        return visible

    def has_sent_to(self, account: str, address: str) -> bool:
        """Gmail's ``in:sent to:<address>``: whether the user ever sent from this mailbox to the
        address. Like Gmail's search, ``to:`` counts To, Cc and Bcc."""
        key = address.strip().lower()
        ids = [
            str(row[0])
            for row in self._db.execute(
                "SELECT message_id FROM message_participants WHERE address=? AND account=? AND direction='SENT' AND role IN ('to', 'cc') "
                "UNION SELECT message_id FROM gmail_sent_bcc WHERE address=? AND account=?",
                (key, account, key, account),
            )
        ]
        return bool(self._visible(account, ids))

    def sent_since(self, account: str, since: datetime, limit: int) -> list[dict[str, Any]]:
        """``in:sent after:<since>``: the newest ``limit`` sent messages, full, newest first."""
        rows = self._db.execute(
            "SELECT message_id FROM messages WHERE account=? AND direction='SENT' AND ts >= ? ORDER BY ts DESC",
            (account, int(since.timestamp())),
        ).fetchall()
        ids = [str(row[0]) for row in rows]
        visible = self._visible(account, ids)
        chosen = sorted((i for i in ids if i in visible), key=_sort_key, reverse=True)[:limit]
        stored = self.messages(account, chosen)
        return [stored[i] for i in chosen if i in stored]

    def sent_containing(self, account: str, needles: Sequence[str], since: datetime) -> list[dict[str, Any]]:
        """Sent messages since then whose text holds any of the strings, full, newest first."""
        if not needles:
            return []
        clauses = " OR ".join("text LIKE ?" for _ in needles)
        rows = self._db.execute(
            f"SELECT message_id FROM messages WHERE account=? AND direction='SENT' AND ts >= ? AND ({clauses}) ORDER BY ts DESC",
            (account, int(since.timestamp()), *[f"%{n}%" for n in needles]),
        ).fetchall()
        ids = [str(row[0]) for row in rows]
        visible = self._visible(account, ids)
        stored = self.messages(account, [i for i in ids if i in visible])
        return [stored[i] for i in ids if i in stored]

    def latest_with(self, account: str, address: str, since: datetime) -> dict[str, Any] | None:
        """``(from:X OR to:X OR cc:X) after:<since> -in:drafts``, newest one: its id, thread and date."""
        key = address.strip().lower()
        rows = self._db.execute(
            "SELECT message_id FROM (SELECT message_id, ts FROM message_participants WHERE address=? AND account=? AND ts >= ? "
            "ORDER BY ts DESC LIMIT 50) UNION SELECT message_id FROM gmail_sent_bcc WHERE address=? AND account=?",
            (key, account, int(since.timestamp()), key, account),
        ).fetchall()
        ids = [str(row[0]) for row in rows]
        visible = self._visible(account, ids)
        stored = self.messages(account, [i for i in ids if i in visible])
        floor_ms = int(since.timestamp() * 1000)
        candidates = [m for m in stored.values() if "DRAFT" not in m["labelIds"] and int(m.get("internalDate", "0") or 0) >= floor_ms]
        if not candidates:
            return None
        newest = max(candidates, key=lambda m: _sort_key(str(m["id"])))
        return {"id": newest["id"], "threadId": newest.get("threadId", newest["id"]), "internalDate": newest.get("internalDate", "0")}

    # --- what changed ---

    def latest_change(self, account: str) -> int:
        row = self._db.execute("SELECT MAX(seq) FROM gmail_changes WHERE account=?", (account,)).fetchone()
        return int(row[0] or 0)

    def changes_since(self, account: str, cursor: int) -> list[dict[str, Any]] | None:
        """Every change after ``cursor`` (a seq from latest_change), oldest first; None when the
        feed no longer reaches back to the cursor, so the caller must treat everything as changed."""
        oldest_kept = self._db.execute("SELECT MIN(seq) FROM gmail_changes").fetchone()[0]
        if oldest_kept is not None and cursor < int(oldest_kept) - 1:
            return None
        return [
            {"seq": int(seq), "id": str(mid), "threadId": str(tid), "kind": str(kind).lower(), "labelIds": json.loads(labels)}
            for seq, mid, tid, kind, labels in self._db.execute(
                "SELECT seq, message_id, thread_id, kind, label_ids_json FROM gmail_changes WHERE account=? AND seq > ? ORDER BY seq",
                (account, cursor),
            )
        ]


def _metadata_shape(raw: dict[str, Any], wanted: set[str]) -> dict[str, Any]:
    payload = raw.get("payload") or {}
    headers = [h for h in payload.get("headers", []) or [] if str(h.get("name", "")).lower() in wanted]
    shaped = {key: raw[key] for key in ("id", "threadId", "labelIds", "snippet", "sizeEstimate", "historyId", "internalDate") if key in raw}
    shaped["payload"] = {"mimeType": payload.get("mimeType", ""), "headers": headers}
    return shaped


def open_for(account: str, consumer: str, needs_labels: bool) -> GmailStore | None:
    """The store when it can answer for this account now; otherwise None, with the reason logged
    (the caller then reads Gmail itself, as it did before the store existed)."""
    store = open_store()
    if store is None:
        reason = "no synced mail store"
    else:
        reason = store.unusable_reason(account, needs_labels)
        if reason is None:
            return store
        store.close()
    log_fallback(consumer, account, reason)
    return None


def open_store() -> GmailStore | None:
    """The store, or None when there is no readable archive (callers then read Gmail)."""
    try:
        return GmailStore()
    except StoreUnavailableError:
        return None
