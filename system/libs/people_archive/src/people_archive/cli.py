import fcntl
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Final

import click
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from loguru import logger
from pydantic import Field

from people_archive.apple_notes_pull import (
    DEFAULT_SELECTION_PATH as DEFAULT_APPLE_NOTES_SELECTION,
)
from people_archive.apple_notes_pull import (
    AppleNotesClient,
    EssaySelection,
    pull_apple_notes,
)
from people_archive.beeper_pull import BeeperClient, pull_beeper
from people_archive.calendar_pull import pull_calendar
from people_archive.contacts_pull import pull_contacts
from people_archive.gateway import LatchkeyGateway
from people_archive.gmail_pull import (
    DEFAULT_FETCH_WORKERS,
    is_full_pull_done,
    pull_gmail_full,
    pull_gmail_incremental,
)
from people_archive.gmail_sync import sync_gmail_accounts
from people_archive.granola_import import import_granola
from people_archive.identity import resolve_and_store_people
from people_archive.judge import JUDGE_MODEL, judge_unjudged_people
from people_archive.merge_judge import MERGE_JUDGE_MODEL, judge_merge_candidates
from people_archive.notes_pull import MacFilesClient, default_vault_folders, pull_notes
from people_archive.primitives import AccountEmail
from people_archive.resolve_lock import resolve_lock
from people_archive.settings import load_settings, require_feature
from people_archive.social_enrichment import (
    BrightDataClient,
    discover_social_profiles,
    harvest_known_profiles,
    run_enrichment_pass,
)
from people_archive.social_private_discovery import run_private_discovery
from people_archive.social_public_discovery import run_public_pilot_locked
from people_archive.store import (
    BATCH_LOCK_WAIT_SECONDS,
    open_archive_store,
    open_existing_archive_store,
)
from people_archive.synthesis import SYNTHESIS_MODEL, synthesize_people_by_name
from people_archive.tagger import TAGGER_MODEL, tag_untagged_people
from people_archive.thread_judge import THREAD_JUDGE_MODEL, judge_unnamed_threads
from people_archive.vault_writer import write_vault

DEFAULT_STORE_PATH: Final[Path] = Path(os.environ.get("CRM_DATA_DIR", "data/.apps/crm")) / "archive.sqlite"
# The sync runs every 5 minutes; this leaves its run room to finish its last step and exit.
DEFAULT_SYNC_SECONDS: Final[int] = 180


class PullGmailArguments(FrozenModel):
    """Parsed arguments of ``pull-gmail``."""

    store_path: Path = Field(description="SQLite archive path")
    account: AccountEmail = Field(description="Which connected Gmail account to pull")
    workers: int = Field(description="Concurrent message fetches")
    is_forced_full: bool = Field(
        description="Walk the whole mailbox even if a full pull was done before"
    )


def _ingest_log_path(store_path: Path) -> Path:
    return store_path.with_suffix(".ingest.jsonl")


@click.group()
def main() -> None:
    """The raw archive behind the CRM."""
    setup_logging(level="INFO")


@main.command("pull-gmail")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option("--account", required=True, help="Connected Gmail account to pull")
@click.option(
    "--workers",
    default=DEFAULT_FETCH_WORKERS,
    show_default=True,
    help="Concurrent message fetches",
)
@click.option(
    "--full/--incremental",
    "is_forced_full",
    default=False,
    help="Force a full walk of the mailbox",
)
def pull_gmail_command(
    store_path: str, account: str, workers: int, is_forced_full: bool
) -> None:
    """Pull one Gmail account into the archive (full walk the first time, incremental after)."""
    arguments = PullGmailArguments(
        store_path=Path(store_path),
        account=AccountEmail(account),
        workers=workers,
        is_forced_full=is_forced_full,
    )
    gateway = LatchkeyGateway.from_environment()
    ingest_log = _ingest_log_path(arguments.store_path)
    with open_archive_store(arguments.store_path) as store:
        is_full = arguments.is_forced_full or not is_full_pull_done(
            store, arguments.account
        )
        logger.info(
            "Pulling Gmail for {} ({})",
            arguments.account,
            "full walk" if is_full else "incremental",
        )
        if is_full:
            summary = pull_gmail_full(
                gateway, store, arguments.account, arguments.workers, ingest_log
            )
        else:
            summary = pull_gmail_incremental(
                gateway, store, arguments.account, arguments.workers, ingest_log
            )
    click.echo(summary.model_dump_json())


@main.command("sync-gmail")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option("--account", "accounts", required=True, multiple=True, help="Gmail account to sync (repeatable)")
@click.option(
    "--time-limit",
    "time_limit_seconds",
    default=DEFAULT_SYNC_SECONDS,
    show_default=True,
    help="Seconds after which no new work starts; the rest continues next run",
)
@click.option(
    "--history-only",
    "is_history_only",
    is_flag=True,
    help="Only apply what changed (a quick catch-up before an app reads the store)",
)
@click.option(
    "--wait",
    "wait_seconds",
    default=0,
    show_default=True,
    help="Seconds to wait for a sync already running to finish (0: leave it to that one)",
)
def sync_gmail_command(
    store_path: str, accounts: tuple[str, ...], time_limit_seconds: int, is_history_only: bool, wait_seconds: int
) -> None:
    """Bring the archive's copy of each mailbox, and every message's labels, up to date."""
    path = Path(store_path)
    lock_path = path.with_name(path.name + ".gmail-sync.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        if not _take_lock(lock, wait_seconds):
            logger.info("Another Gmail sync is running; leaving it to finish")
            return
        deadline = datetime.now(timezone.utc) + timedelta(seconds=time_limit_seconds)
        gateway = LatchkeyGateway.from_environment()
        with open_archive_store(path) as store:
            summaries = sync_gmail_accounts(
                gateway,
                store,
                [AccountEmail(a) for a in accounts],
                deadline,
                _ingest_log_path(path),
                is_history_only=is_history_only,
            )
    for summary in summaries:
        click.echo(summary.model_dump_json())
    behind = [str(s.account) for s in summaries if not s.is_caught_up]
    if behind:
        raise click.ClickException(f"Gmail sync did not catch up for: {', '.join(behind)}")


def _take_lock(lock: IO[str], wait_seconds: int) -> bool:
    """Hold the sync lock, waiting up to wait_seconds for a run in progress to release it."""
    give_up_at = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
    pause = threading.Event()
    is_held = False
    while not is_held:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            is_held = True
        except BlockingIOError:
            if datetime.now(timezone.utc) >= give_up_at:
                return False
            pause.wait(0.5)
    return True


@main.command("pull-beeper")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
def pull_beeper_command(store_path: str) -> None:
    """Pull every chat and message from Beeper Desktop on the Mac (full history first, then only new)."""
    client = BeeperClient.from_environment()
    with open_archive_store(Path(store_path)) as store:
        summary = pull_beeper(client, store)
    click.echo(summary.model_dump_json())


@main.command("pull-notes")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
def pull_notes_command(store_path: str) -> None:
    """Read every note in the Obsidian vault and the Logseq graphs on the Mac (read-only)."""
    client = MacFilesClient.from_environment()
    with open_archive_store(Path(store_path)) as store:
        summary = pull_notes(client, store, default_vault_folders())
    click.echo(summary.model_dump_json())


@main.command("pull-apple-notes")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option(
    "--selection",
    "selection_path",
    default=str(DEFAULT_APPLE_NOTES_SELECTION),
    show_default=True,
    help="Which Apple Notes are essay topics",
)
def pull_apple_notes_command(store_path: str, selection_path: str) -> None:
    """Read the Apple Notes essay topics from the Mac (read-only; the database copy is deleted after)."""
    selection = EssaySelection.load(Path(selection_path))
    client = AppleNotesClient.from_environment(selection.device)
    with open_archive_store(Path(store_path)) as store:
        summary = pull_apple_notes(client, store, selection)
    click.echo(summary.model_dump_json())


@main.command("import-granola")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.argument("csv_path")
def import_granola_command(store_path: str, csv_path: str) -> None:
    """Load a Granola CSV export: every meeting with its summary and transcript, matched to the calendar."""
    with open_archive_store(Path(store_path)) as store:
        summary = import_granola(store, Path(csv_path))
    click.echo(summary.model_dump_json())


@main.command("pull-contacts")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option(
    "--account", required=True, help="Connected Google account whose contacts to pull"
)
def pull_contacts_command(store_path: str, account: str) -> None:
    """Pull one account's Google Contacts into the archive."""
    gateway = LatchkeyGateway.from_environment()
    with open_archive_store(Path(store_path)) as store:
        summary = pull_contacts(gateway, store, AccountEmail(account))
    click.echo(summary.model_dump_json())


@main.command("pull-calendar")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option(
    "--account",
    required=True,
    help="Connected Google account whose primary calendar to pull",
)
def pull_calendar_command(store_path: str, account: str) -> None:
    """Pull one account's primary calendar into the archive (full from 2019 the first time, then incremental)."""
    gateway = LatchkeyGateway.from_environment()
    with open_archive_store(Path(store_path)) as store:
        summary = pull_calendar(gateway, store, AccountEmail(account))
    click.echo(summary.model_dump_json())


@main.command("resolve")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option(
    "--account",
    "accounts",
    multiple=True,
    default=lambda: tuple(load_settings().accounts),
    help="Your account emails (repeatable; defaults to settings)",
)
@click.option(
    "--claim-marker",
    "claim_marker",
    default="",
    help="A file to delete once this run holds the rebuild lock and is about to read the ledgers; "
    "whoever queued the run leaves it there so it does not queue another",
)
def resolve_command(
    store_path: str, accounts: tuple[str, ...], claim_marker: str
) -> None:
    """Rebuild the people tables from everything in the archive. Waits for any rebuild already running."""
    with resolve_lock(Path(store_path)):
        if claim_marker:
            Path(claim_marker).unlink(missing_ok=True)
        with open_archive_store(Path(store_path), BATCH_LOCK_WAIT_SECONDS) as store:
            people = resolve_and_store_people(
                store, [AccountEmail(a) for a in accounts]
            )
    humans = [p for p in people if p.is_human]
    click.echo(
        json.dumps(
            {
                "people": len(people),
                "human": len(humans),
                "tier_1": sum(1 for p in humans if p.tier == 1),
                "tier_2": sum(1 for p in humans if p.tier == 2),
                "tier_3": sum(1 for p in humans if p.tier == 3),
            }
        )
    )


@main.command("write-vault")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option(
    "--vault",
    "vault_root",
    required=True,
    help="Folder to write People/, index.md, CLAUDE.md, and log.md into",
)
@click.option(
    "--max-tier",
    default=2,
    show_default=True,
    help="Write pages for people up to this tier",
)
def write_vault_command(store_path: str, vault_root: str, max_tier: int) -> None:
    """Write one Markdown page per person into the vault folder, preserving anything human-written."""
    with open_archive_store(Path(store_path)) as store:
        summary = write_vault(store, Path(vault_root), max_tier)
    click.echo(summary.model_dump_json())


@main.command("judge")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option(
    "--model", default=JUDGE_MODEL, show_default=True, help="Model that judges"
)
@click.option(
    "--limit", default=2000, show_default=True, help="Most people to judge in one run"
)
def judge_command(store_path: str, model: str, limit: int) -> None:
    """Ask an AI judge which unjudged entries are real people (vs groups and automations)."""
    with open_archive_store(Path(store_path), BATCH_LOCK_WAIT_SECONDS) as store:
        judged = judge_unjudged_people(store, model, limit)
        verdicts = store.execute_read(
            "SELECT verdict, COUNT(*) FROM person_judgments GROUP BY verdict", []
        )
    click.echo(
        json.dumps(
            {"judged_now": judged, "totals": {str(v): int(n) for v, n in verdicts}}
        )
    )


@main.command("judge-merges")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option(
    "--model", default=MERGE_JUDGE_MODEL, show_default=True, help="Model that judges"
)
@click.option(
    "--limit", default=2000, show_default=True, help="Most pairs to judge in one run"
)
@click.option(
    "--rejudge", is_flag=True, help="Judge pairs again even when a judgment exists"
)
def judge_merges_command(
    store_path: str, model: str, limit: int, rejudge: bool
) -> None:
    """Ask an AI judge which possible-duplicate pairs are one person; confident calls apply at the next resolve."""
    with open_archive_store(Path(store_path), BATCH_LOCK_WAIT_SECONDS) as store:
        summary = judge_merge_candidates(store, model, limit, rejudge)
    click.echo(summary.model_dump_json())


@main.command("judge-threads")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option(
    "--model", default=THREAD_JUDGE_MODEL, show_default=True, help="Model that judges"
)
@click.option(
    "--limit", default=3000, show_default=True, help="Most threads to judge in one run"
)
@click.option(
    "--rejudge", is_flag=True, help="Judge threads again even when a judgment exists"
)
def judge_threads_command(
    store_path: str, model: str, limit: int, rejudge: bool
) -> None:
    """Read the text thread behind each unnamed number: known person, stranger, or bot; applies at the next resolve."""
    with open_archive_store(Path(store_path), BATCH_LOCK_WAIT_SECONDS) as store:
        summary = judge_unnamed_threads(store, model, limit, rejudge)
    click.echo(summary.model_dump_json())


@main.command("synthesize")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option(
    "--model",
    default=SYNTHESIS_MODEL,
    show_default=True,
    help="Model that writes the dossier prose",
)
@click.option(
    "--name",
    "names",
    multiple=True,
    required=True,
    help="Canonical name of a person (repeatable)",
)
def synthesize_command(store_path: str, model: str, names: tuple[str, ...]) -> None:
    """Write the model-authored dossier sections for the named people from their evidence."""
    with open_archive_store(Path(store_path)) as store:
        written = synthesize_people_by_name(store, list(names), model)
    for person_id, dossier in written:
        click.echo(f"{person_id}: {dossier.one_liner}")


@main.command("tag")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option(
    "--model", default=TAGGER_MODEL, show_default=True, help="Model that tags"
)
@click.option(
    "--limit", default=5000, show_default=True, help="Most people to tag in one run"
)
def tag_command(store_path: str, model: str, limit: int) -> None:
    """Tag close-or-known people with interests, background, reach, and launch fit (cheap, wide)."""
    with open_archive_store(Path(store_path)) as store:
        tagged = tag_untagged_people(store, model, limit)
        totals = store.execute_read(
            "SELECT reach, fit, COUNT(*) FROM person_tags GROUP BY reach, fit", []
        )
    click.echo(
        json.dumps(
            {
                "tagged_now": tagged,
                "totals": [[str(a), str(b), int(n)] for a, b, n in totals],
            }
        )
    )


@main.command("enrich-social")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option(
    "--limit",
    default=10,
    show_default=True,
    help="Most confirmed profile URLs to fetch in this pass",
)
@click.option(
    "--prepare-only",
    is_flag=True,
    help="Only queue people and harvest existing handles/signature links",
)
@click.option(
    "--status",
    "show_status",
    is_flag=True,
    help="Print current enrichment counts without changing anything",
)
@click.option(
    "--counts-only",
    is_flag=True,
    help="Fetch counts for already confirmed profiles without looking for new handles",
)
@click.option(
    "--workers",
    default=1,
    type=click.IntRange(1, 4),
    show_default=True,
    help="Concurrent Bright Data requests for confirmed profiles",
)
def enrich_social_command(
    store_path: str,
    limit: int,
    prepare_only: bool,
    show_status: bool,
    counts_only: bool,
    workers: int,
) -> None:
    """Enrich public LinkedIn, X, and Instagram profiles in Close/Known/Peripheral order."""
    require_feature("social")
    if prepare_only and counts_only:
        raise click.UsageError("--counts-only cannot be combined with --prepare-only")
    if show_status or prepare_only:
        _enrich_social_locked(
            store_path, limit, prepare_only, show_status, counts_only, workers
        )
        return
    lock_path = Path(store_path).with_suffix(".brightdata-fetch.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as fetch_lock:
        fcntl.flock(fetch_lock, fcntl.LOCK_EX)
        _enrich_social_locked(
            store_path, limit, prepare_only, show_status, counts_only, workers
        )


def _enrich_social_locked(
    store_path: str,
    limit: int,
    prepare_only: bool,
    show_status: bool,
    counts_only: bool,
    workers: int,
) -> None:
    with open_archive_store(Path(store_path)) as store:
        if show_status:
            click.echo(
                json.dumps(store.social_enrichment_status(), indent=2, sort_keys=True)
            )
            return
        if prepare_only:
            prepared_at = datetime.now(timezone.utc)
            queued_now = store.refresh_social_enrichment_queue(prepared_at)
            exact, signatures = harvest_known_profiles(store, prepared_at)
            store.set_social_enrichment_metadata(
                "social_harvest_after", (prepared_at + timedelta(hours=12)).isoformat()
            )
            click.echo(
                json.dumps(
                    {
                        "queued_now": queued_now,
                        "exact_profiles_processed": exact,
                        "signature_candidates_processed": signatures,
                        "totals": store.social_enrichment_status(),
                    },
                    sort_keys=True,
                )
            )
            return
        summary = run_enrichment_pass(
            store,
            BrightDataClient(),
            limit=max(1, limit),
            harvest_known=not counts_only,
            workers=workers,
        )
    click.echo(summary.model_dump_json())


@main.command("discover-social")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option(
    "--limit",
    default=3,
    show_default=True,
    help="Most people to search in one GPT batch",
)
def discover_social_command(store_path: str, limit: int) -> None:
    """Find high-confidence social profiles with GPT web search, Close people first."""
    require_feature("social")
    with open_existing_archive_store(Path(store_path)) as store:
        result = discover_social_profiles(store, max(1, limit))
    click.echo(json.dumps(result, indent=2, sort_keys=True))


@main.command("discover-social-private")
@click.option("--store", "store_path", default=str(DEFAULT_STORE_PATH))
@click.option("--limit", default=1000, show_default=True)
@click.option(
    "--colleague-backfill",
    is_flag=True,
    help="Revisit previously scanned messages from human colleagues",
)
def discover_social_private_command(
    store_path: str, limit: int, colleague_backfill: bool
) -> None:
    """Find missing profiles in the sender's own archived messages without paid search."""
    require_feature("social")
    with open_existing_archive_store(Path(store_path)) as store:
        result = run_private_discovery(
            store, limit=max(1, limit), colleague_backfill=colleague_backfill
        )
    click.echo(json.dumps(result, sort_keys=True))


@main.command("discover-social-public-pilot")
@click.option("--store", "store_path", default=str(DEFAULT_STORE_PATH))
def discover_social_public_pilot_command(store_path: str) -> None:
    """Advance one capped public-search pilot input or pending result."""
    require_feature("social")
    result = run_public_pilot_locked(Path(store_path))
    click.echo(json.dumps(result, sort_keys=True))


@main.command("status")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
def status_command(store_path: str) -> None:
    """Counts and freshness of the archive, as JSON."""
    with open_archive_store(Path(store_path)) as store:
        click.echo(json.dumps(store.status().model_dump(mode="json"), indent=2))


@main.command("search")
@click.option(
    "--store",
    "store_path",
    default=str(DEFAULT_STORE_PATH),
    show_default=True,
    help="SQLite archive path",
)
@click.option("--limit", default=20, show_default=True, help="Maximum hits")
@click.argument("query")
def search_command(store_path: str, limit: int, query: str) -> None:
    """Full-text search over every archived message."""
    with open_archive_store(Path(store_path)) as store:
        for hit in store.search_messages(query, limit):
            click.echo(
                f"{hit.timestamp:%Y-%m-%d} {hit.direction.value:8} {hit.from_name or hit.from_addr:30.30} {hit.subject[:60]}"
            )
            click.echo(f"    {hit.snippet[:160]}")


if __name__ == "__main__":
    main()
