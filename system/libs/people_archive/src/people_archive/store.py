import json
import sqlite3
import zlib
from collections.abc import Iterator, Sequence
from collections.abc import Set as AbstractSet
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from pydantic import Field, PrivateAttr

from people_archive.data_types import (
    CalendarEvent,
    ContactCard,
    Direction,
    EventAttendee,
    GmailChangeKind,
    GmailLabelRow,
    MessageParticipant,
    ParsedEmail,
)
from people_archive.errors import StoreError
from people_archive.primitives import (
    AccountEmail,
    CalendarEventId,
    ContactResourceName,
    EmailAddress,
    GmailMessageId,
    GmailThreadId,
    HandleKey,
    PersonId,
    PhoneNumber,
    SourceKind,
)

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS raw_records (
    source TEXT NOT NULL,
    account TEXT NOT NULL,
    record_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    raw_zlib BLOB NOT NULL,
    PRIMARY KEY (source, account, record_id)
);
CREATE TABLE IF NOT EXISTS sync_state (
    source TEXT NOT NULL,
    account TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (source, account, key)
);
CREATE TABLE IF NOT EXISTS messages (
    account TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    direction TEXT NOT NULL,
    from_name TEXT NOT NULL,
    from_addr TEXT NOT NULL,
    to_json TEXT NOT NULL,
    cc_json TEXT NOT NULL,
    subject TEXT NOT NULL,
    snippet TEXT NOT NULL,
    text TEXT NOT NULL,
    label_ids_json TEXT NOT NULL,
    is_automated INTEGER NOT NULL,
    rfc_message_id TEXT NOT NULL,
    in_reply_to TEXT NOT NULL,
    PRIMARY KEY (account, message_id)
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages (ts);
CREATE INDEX IF NOT EXISTS messages_thread ON messages (account, thread_id);
CREATE TABLE IF NOT EXISTS message_participants (
    account TEXT NOT NULL,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    address TEXT NOT NULL,
    name TEXT NOT NULL,
    ts INTEGER NOT NULL,
    direction TEXT NOT NULL,
    PRIMARY KEY (account, message_id, role, address)
);
CREATE INDEX IF NOT EXISTS message_participants_address ON message_participants (address, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5 (
    subject, text, from_name, from_addr,
    content='messages', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, subject, text, from_name, from_addr)
    VALUES (new.rowid, new.subject, new.text, new.from_name, new.from_addr);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, subject, text, from_name, from_addr)
    VALUES ('delete', old.rowid, old.subject, old.text, old.from_name, old.from_addr);
END;
CREATE TABLE IF NOT EXISTS contacts (
    account TEXT NOT NULL,
    resource_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    emails_json TEXT NOT NULL,
    phones_json TEXT NOT NULL,
    organization TEXT NOT NULL,
    title TEXT NOT NULL,
    PRIMARY KEY (account, resource_name)
);
CREATE TABLE IF NOT EXISTS calendar_events (
    account TEXT NOT NULL,
    event_id TEXT NOT NULL,
    series_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    organizer TEXT NOT NULL,
    attendees_json TEXT NOT NULL,
    is_organized_by_owner INTEGER NOT NULL,
    PRIMARY KEY (account, event_id)
);
CREATE INDEX IF NOT EXISTS calendar_events_start ON calendar_events (start_ts);
CREATE TABLE IF NOT EXISTS beeper_chats (
    chat_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    network TEXT NOT NULL,
    chat_type TEXT NOT NULL,
    title TEXT NOT NULL,
    participants_json TEXT NOT NULL,
    last_activity_ts INTEGER
);
CREATE TABLE IF NOT EXISTS beeper_participants (
    chat_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    handle_key TEXT NOT NULL,
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    username TEXT NOT NULL,
    is_self INTEGER NOT NULL,
    PRIMARY KEY (chat_id, participant_id)
);
CREATE INDEX IF NOT EXISTS beeper_participants_handle ON beeper_participants (handle_key);
CREATE TABLE IF NOT EXISTS beeper_messages (
    message_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    is_sender INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS beeper_messages_chat_ts ON beeper_messages (chat_id, ts);
CREATE INDEX IF NOT EXISTS beeper_messages_ts ON beeper_messages (ts);
CREATE VIRTUAL TABLE IF NOT EXISTS beeper_messages_fts USING fts5 (
    text, sender_name, content='beeper_messages', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS beeper_messages_fts_insert AFTER INSERT ON beeper_messages BEGIN
    INSERT INTO beeper_messages_fts (rowid, text, sender_name) VALUES (new.rowid, new.text, new.sender_name);
END;
CREATE TRIGGER IF NOT EXISTS beeper_messages_fts_delete AFTER DELETE ON beeper_messages BEGIN
    INSERT INTO beeper_messages_fts (beeper_messages_fts, rowid, text, sender_name) VALUES ('delete', old.rowid, old.text, old.sender_name);
END;
CREATE TABLE IF NOT EXISTS beeper_chat_sync (
    chat_id TEXT PRIMARY KEY,
    newest_sort_key TEXT NOT NULL,
    is_history_complete INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    source TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    is_journal INTEGER NOT NULL,
    note_date_ts INTEGER,
    text TEXT NOT NULL,
    mentions_json TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    properties_json TEXT NOT NULL,
    PRIMARY KEY (source, path)
);
CREATE TABLE IF NOT EXISTS note_mentions (
    source TEXT NOT NULL,
    path TEXT NOT NULL,
    handle_key TEXT NOT NULL,
    PRIMARY KEY (source, path, handle_key)
);
CREATE INDEX IF NOT EXISTS note_mentions_handle ON note_mentions (handle_key);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5 (
    title, text, content='notes', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS notes_fts_insert AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts (rowid, title, text) VALUES (new.rowid, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS notes_fts_delete AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts (notes_fts, rowid, title, text) VALUES ('delete', old.rowid, old.title, old.text);
END;
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    started_ts INTEGER NOT NULL,
    summary TEXT NOT NULL,
    notes TEXT NOT NULL,
    transcript TEXT NOT NULL,
    calendar_event_id TEXT NOT NULL,
    attendee_emails_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS meetings_started ON meetings (started_ts);
CREATE VIRTUAL TABLE IF NOT EXISTS meetings_fts USING fts5 (
    title, summary, transcript, content='meetings', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS meetings_fts_insert AFTER INSERT ON meetings BEGIN
    INSERT INTO meetings_fts (rowid, title, summary, transcript) VALUES (new.rowid, new.title, new.summary, new.transcript);
END;
CREATE TRIGGER IF NOT EXISTS meetings_fts_delete AFTER DELETE ON meetings BEGIN
    INSERT INTO meetings_fts (meetings_fts, rowid, title, summary, transcript) VALUES ('delete', old.rowid, old.title, old.summary, old.transcript);
END;
CREATE TABLE IF NOT EXISTS person_synthesis (
    person_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    synthesized_at TEXT NOT NULL,
    excerpt_count INTEGER NOT NULL,
    one_liner TEXT NOT NULL,
    key_facts TEXT NOT NULL DEFAULT '',
    how_we_met TEXT NOT NULL,
    what_they_care_about TEXT NOT NULL,
    where_things_stand TEXT NOT NULL,
    watch_out TEXT NOT NULL DEFAULT '',
    interests_json TEXT NOT NULL,
    background_json TEXT NOT NULL,
    next_steps TEXT NOT NULL,
    cost_usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS person_tags (
    person_id TEXT PRIMARY KEY,
    interests_json TEXT NOT NULL,
    background_json TEXT NOT NULL,
    reach TEXT NOT NULL,
    reach_why TEXT NOT NULL,
    fit TEXT NOT NULL,
    fit_why TEXT NOT NULL,
    tagged_at TEXT NOT NULL,
    model TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS social_profile_sources (
    source_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    source_url TEXT NOT NULL,
    provider TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL,
    raw_zlib BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS social_profile_sources_person ON social_profile_sources (person_id, platform, fetched_at);
CREATE TABLE IF NOT EXISTS social_profile_candidates (
    person_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    profile_url TEXT NOT NULL,
    username TEXT NOT NULL,
    discovered_via TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (person_id, platform, profile_url)
);
CREATE INDEX IF NOT EXISTS social_profile_candidates_state ON social_profile_candidates (state, platform, confidence);
CREATE TABLE IF NOT EXISTS social_profiles (
    person_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    profile_url TEXT NOT NULL,
    username TEXT NOT NULL,
    display_name TEXT NOT NULL,
    follower_count INTEGER,
    following_count INTEGER,
    connection_count INTEGER,
    biography TEXT NOT NULL,
    headline TEXT NOT NULL,
    is_verified INTEGER,
    confidence REAL NOT NULL,
    match_reason TEXT NOT NULL,
    discovered_via TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source_id TEXT NOT NULL,
    PRIMARY KEY (person_id, platform)
);
CREATE INDEX IF NOT EXISTS social_profiles_followers ON social_profiles (platform, follower_count DESC);
CREATE TABLE IF NOT EXISTS social_enrichment_queue (
    person_id TEXT PRIMARY KEY,
    tier INTEGER NOT NULL,
    score INTEGER NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS social_enrichment_queue_order ON social_enrichment_queue (tier, score DESC, state, next_attempt_at);
CREATE TABLE IF NOT EXISTS social_enrichment_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS social_discovery_status (
    person_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    profiles_found INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    searched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS list_decisions (
    list_name TEXT NOT NULL,
    person_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (list_name, person_id)
);
CREATE TABLE IF NOT EXISTS person_months (
    person_id TEXT NOT NULL,
    month TEXT NOT NULL,
    emails INTEGER NOT NULL,
    meetings INTEGER NOT NULL,
    text_days INTEGER NOT NULL,
    PRIMARY KEY (person_id, month)
);
CREATE TABLE IF NOT EXISTS person_edits (
    person_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    original TEXT NOT NULL,
    edited_at TEXT NOT NULL,
    PRIMARY KEY (person_id, field)
);
CREATE TABLE IF NOT EXISTS persons (
    person_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    emails_json TEXT NOT NULL,
    phones_json TEXT NOT NULL,
    organization TEXT NOT NULL,
    title TEXT NOT NULL,
    tier INTEGER NOT NULL,
    is_human INTEGER NOT NULL,
    is_colleague INTEGER NOT NULL,
    hand_kept_category TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL,
    received_human_count INTEGER NOT NULL,
    received_automated_count INTEGER NOT NULL,
    sent_count INTEGER NOT NULL,
    meeting_count INTEGER NOT NULL,
    text_days_sent INTEGER NOT NULL DEFAULT 0,
    text_days_received INTEGER NOT NULL DEFAULT 0,
    first_contact_ts INTEGER,
    last_contact_ts INTEGER
);
CREATE INDEX IF NOT EXISTS persons_tier ON persons (is_human, tier, score);
CREATE INDEX IF NOT EXISTS persons_list_score ON persons (is_human, is_colleague, score DESC, last_contact_ts DESC, tier);
CREATE INDEX IF NOT EXISTS persons_list_recent ON persons (is_human, is_colleague, last_contact_ts DESC, score DESC, tier);
CREATE TABLE IF NOT EXISTS person_handles (
    handle_key TEXT PRIMARY KEY,
    person_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS person_handles_person ON person_handles (person_id);
CREATE TABLE IF NOT EXISTS thread_judgments (
    handle_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    who TEXT NOT NULL,
    confidence REAL NOT NULL,
    judged_at TEXT NOT NULL,
    model TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS person_judgments (
    person_id TEXT PRIMARY KEY,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL,
    judged_at TEXT NOT NULL,
    model TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_judgments (
    pair_key TEXT PRIMARY KEY,
    handles_a_json TEXT NOT NULL,
    handles_b_json TEXT NOT NULL,
    name TEXT NOT NULL,
    verdict TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT NOT NULL,
    judged_at TEXT NOT NULL,
    model TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_candidates (
    person_id_a TEXT NOT NULL,
    person_id_b TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (person_id_a, person_id_b)
);
CREATE VIRTUAL TABLE IF NOT EXISTS calendar_events_fts USING fts5 (
    summary, attendees_json,
    content='calendar_events', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS calendar_events_fts_insert AFTER INSERT ON calendar_events BEGIN
    INSERT INTO calendar_events_fts (rowid, summary, attendees_json)
    VALUES (new.rowid, new.summary, new.attendees_json);
END;
CREATE TRIGGER IF NOT EXISTS calendar_events_fts_delete AFTER DELETE ON calendar_events BEGIN
    INSERT INTO calendar_events_fts (calendar_events_fts, rowid, summary, attendees_json)
    VALUES ('delete', old.rowid, old.summary, old.attendees_json);
END;
-- Gmail's current labels per message, kept by the Gmail sync (gmail_sync.py) from Gmail's change
-- history, so "what is in the inbox now" is answered here. sort_key orders messages the way Gmail
-- lists them (newest first by id); internal_ms is 0 while unknown.
CREATE TABLE IF NOT EXISTS gmail_label_state (
    account TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    sort_key TEXT NOT NULL,
    internal_ms INTEGER NOT NULL,
    label_ids_json TEXT NOT NULL,
    is_inbox INTEGER NOT NULL,
    is_deleted INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account, message_id)
);
CREATE INDEX IF NOT EXISTS gmail_label_state_inbox ON gmail_label_state (account, is_inbox, sort_key);
CREATE INDEX IF NOT EXISTS gmail_label_state_thread ON gmail_label_state (account, thread_id);
-- Every message the sync added, relabelled or saw deleted, in order: readers ask for what changed
-- after the last seq they saw. kind is added, labels or deleted.
CREATE TABLE IF NOT EXISTS gmail_changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    label_ids_json TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS gmail_changes_account ON gmail_changes (account, seq);
-- Who each sent message was Bcc'd to: Gmail's "sent to X" counts Bcc, message_participants does not.
CREATE TABLE IF NOT EXISTS gmail_sent_bcc (
    account TEXT NOT NULL,
    address TEXT NOT NULL,
    message_id TEXT NOT NULL,
    PRIMARY KEY (account, address, message_id)
);
"""

# Fields the user can overwrite in place. The first three live on the persons row itself; the rest are
# dossier sections and tags, kept beside the model's text so hers wins without erasing the original.
PERSON_COLUMN_BY_FIELD: Final[dict[str, str]] = {
    "name": "canonical_name",
    "organization": "organization",
    "title": "title",
}
DOSSIER_FIELDS: Final[tuple[str, ...]] = (
    "one_liner",
    "key_facts",
    "how_we_met",
    "what_they_care_about",
    "where_things_stand",
    "watch_out",
    "interests",
    "background",
    "notes",
)
EDITABLE_FIELDS: Final[frozenset[str]] = frozenset(
    (*PERSON_COLUMN_BY_FIELD, *DOSSIER_FIELDS)
)


@pure
def parse_tag_list(value: str) -> tuple[str, ...]:
    """Tags as the user types them: comma (or line) separated, lowercased, deduplicated, no leading #."""
    seen: list[str] = []
    for raw in value.replace("\n", ",").split(","):
        tag = raw.strip().lower().lstrip("#")
        if tag and tag not in seen:
            seen.append(tag)
    return tuple(seen)


# Bulk loads go faster with WAL and relaxed fsync; durability across a crash is a
# re-pull of the last page, which the ledger of stored ids makes free.
# Several pulls (one per account) write the same file at once; each commits per page, so a
# writer only ever waits for another's short transaction.
# How many index matches a search reads before picking the newest: the index ranks that many
# in well under a second, whereas dating every match of a common word took most of a minute.
SEARCH_CANDIDATES: Final[int] = 1000
# SQLite caps bound parameters per statement; id lists are queried in chunks this size.
_IN_CLAUSE_CHUNK: Final[int] = 500

_PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA busy_timeout=60000",
)
# The default 2 MB page cache is smaller than the persons table, so a page of the people list
# re-read the whole table from disk for every count; 32 MB keeps the derived tables resident.
DEFAULT_CACHE_KIB: Final[int] = 32768


class MessageSearchHit(FrozenModel):
    """One search result over the archive's messages."""

    account: AccountEmail = Field(description="Which inbox")
    message_id: GmailMessageId = Field(description="Gmail message id")
    thread_id: GmailThreadId = Field(description="Gmail thread id")
    timestamp: datetime = Field(description="When, UTC")
    direction: Direction = Field(description="Sent or received")
    from_name: str = Field(description="Sender display name")
    from_addr: str = Field(description="Sender address")
    subject: str = Field(description="Subject")
    snippet: str = Field(description="Highlighted excerpt around the match")
    recipient_addresses: tuple[str, ...] = Field(
        default=(),
        description="To and cc addresses, so the people on the mail can be linked",
    )


class EventSearchHit(FrozenModel):
    """One search result over the archive's calendar events."""

    account: AccountEmail = Field(description="Which calendar")
    event_id: CalendarEventId = Field(description="Google's event id")
    start: datetime = Field(description="When, UTC")
    summary: str = Field(description="Event title")
    attendee_count: int = Field(description="How many people were on the invite")
    attendee_addresses: tuple[str, ...] = Field(
        default=(), description="Who was on the invite, so they can be linked"
    )


class ResolvedPersonRow(FrozenModel):
    """What the store needs to persist one resolved person (the identity pass's output shape)."""

    person_id: PersonId = Field(description="Stable id")
    canonical_name: str = Field(description="Best display name")
    aliases: tuple[str, ...] = Field(description="Other names")
    emails: tuple[str, ...] = Field(description="Email handles")
    phones: tuple[str, ...] = Field(description="Phone handles")
    handle_keys: tuple[HandleKey, ...] = Field(
        description="Every handle key in the cluster"
    )
    organization: str = Field(description="Company")
    title: str = Field(description="Job title")
    tier: int = Field(description="1..3")
    is_human: bool = Field(description="Passed the human gate")
    is_colleague: bool = Field(
        description="Has an address at one of the user's own companies"
    )
    hand_kept_category: str = Field(
        description="Category from the hand-kept contacts file, if any"
    )
    score: int = Field(description="Relationship score")
    received_human_count: int = Field(description="Human messages from them")
    received_automated_count: int = Field(description="Automated messages from them")
    sent_count: int = Field(description="Messages sent to them")
    meeting_count: int = Field(description="Meetings shared")
    text_days_sent: int = Field(description="Days the user texted them")
    text_days_received: int = Field(description="Days they texted the user")
    first_contact: datetime | None = Field(description="Earliest interaction")
    last_contact: datetime | None = Field(description="Latest interaction")


class PersonTagsRow(FrozenModel):
    """Cheap model-assigned tags for one person: topics, roles, reach, and launch fit."""

    person_id: PersonId = Field(description="Whose")
    interests_json: str = Field(description="JSON array of topic tags")
    background_json: str = Field(description="JSON array of role tags")
    reach: str = Field(description="none / some / high")
    reach_why: str = Field(description="One clause")
    fit: str = Field(description="no / maybe / yes -- would they want or amplify Minds")
    fit_why: str = Field(description="One clause")
    tagged_at: datetime = Field(description="When")
    model: str = Field(description="Which model")


class SocialProfileRow(FrozenModel):
    """One public social profile accepted as belonging to a CRM person."""

    person_id: PersonId = Field(description="Whose")
    platform: str = Field(description="linkedin / x / instagram")
    profile_url: str = Field(description="Canonical public profile URL")
    username: str = Field(description="Public username or profile slug")
    display_name: str = Field(default="", description="Name shown by the network")
    follower_count: int | None = Field(
        default=None, description="Public follower count"
    )
    following_count: int | None = Field(
        default=None, description="Public following count"
    )
    connection_count: int | None = Field(
        default=None, description="Public connection count"
    )
    biography: str = Field(default="", description="Public biography")
    headline: str = Field(default="", description="Public role headline")
    is_verified: bool | None = Field(
        default=None, description="Whether the network marks the profile verified"
    )
    confidence: float = Field(
        description="Confidence that the profile belongs to this person"
    )
    match_reason: str = Field(description="Why the identity match was accepted")
    discovered_via: str = Field(description="CRM handle, signature, or public search")
    observed_at: datetime = Field(description="When the public data was observed")
    source_id: str = Field(
        default="", description="Raw-source record backing the latest values"
    )


class SynthesisRow(FrozenModel):
    """The model-written dossier sections for one person, as stored."""

    person_id: PersonId = Field(description="Whose")
    model: str = Field(description="Which model wrote it")
    synthesized_at: datetime = Field(description="When")
    excerpt_count: int = Field(
        description="How many dated excerpts it was written from"
    )
    one_liner: str = Field(description="Who they are to the user")
    key_facts: str = Field(description="Bullets a stranger needs first")
    how_we_met: str = Field(description="Cited bullets")
    what_they_care_about: str = Field(description="Cited bullets")
    where_things_stand: str = Field(description="Cited bullets")
    watch_out: str = Field(description="Cited bullets; may be empty")
    interests_json: str = Field(description="JSON array of topic tags")
    background_json: str = Field(description="JSON array of role tags")
    next_steps: str = Field(description="Suggested next moves")
    cost_usd: float = Field(description="What the call cost")


class MeetingRow(FrozenModel):
    """One recorded meeting (Granola), with the calendar event it matched."""

    meeting_id: str = Field(description="Recorder's id")
    title: str = Field(description="Meeting title")
    started: datetime = Field(description="When, UTC")
    summary: str = Field(description="Recorder's summary")
    notes: str = Field(description="The user's own notes")
    transcript: str = Field(description="Full transcript")
    calendar_event_id: str = Field(description="Matched calendar event id, or empty")
    attendee_emails: tuple[str, ...] = Field(
        description="Attendee emails from the matched event"
    )


class MeetingSearchHit(FrozenModel):
    """One search result over recorded meetings."""

    meeting_id: str = Field(description="Meeting")
    title: str = Field(description="Title")
    started: datetime = Field(description="When")
    snippet: str = Field(description="Excerpt with [ ] around matches")


class NoteRow(FrozenModel):
    """One Markdown note from the user's vaults, with what it mentions."""

    source: str = Field(description="obsidian, or logseq:<graph>")
    path: str = Field(description="Path below /Users on the Mac")
    title: str = Field(description="File name without extension")
    is_journal: bool = Field(description="A dated journal / daily note")
    note_date: datetime | None = Field(description="The journal day, when it is one")
    text: str = Field(description="Full Markdown text")
    mentions: tuple[str, ...] = Field(description="[[Page]] names linked from the note")
    tags: tuple[str, ...] = Field(description="#tags in the note")
    properties: dict[str, str] = Field(description="Frontmatter / Logseq properties")


class NoteSearchHit(FrozenModel):
    """One search result over notes."""

    source: str = Field(description="Which vault")
    path: str = Field(description="Path on the Mac")
    title: str = Field(description="Note title")
    note_date: datetime | None = Field(description="Journal day, if any")
    snippet: str = Field(description="Excerpt with [ ] around matches")


class BeeperChatRow(FrozenModel):
    """One Beeper chat, projected."""

    chat_id: str = Field(description="Beeper chat id")
    account_id: str = Field(description="Beeper account the chat belongs to")
    network: str = Field(description="iMessage / WhatsApp / Signal / ...")
    chat_type: str = Field(description="single or group")
    title: str = Field(description="Chat title")
    participants_json: str = Field(description="Participants as Beeper returned them")
    last_activity: datetime | None = Field(description="Last activity, UTC")


class BeeperParticipantRow(FrozenModel):
    """One participant of a Beeper chat with the handle the identity pass keys on."""

    chat_id: str = Field(description="Chat")
    participant_id: str = Field(description="Beeper participant id")
    handle_key: HandleKey = Field(
        description="phone:+1..., email:..., or <network>:<username or id>"
    )
    full_name: str = Field(description="Display name; may be empty")
    phone: str = Field(description="Phone if known")
    username: str = Field(description="Network username if known")
    is_self: bool = Field(description="the user")


class BeeperMessageRow(FrozenModel):
    """One Beeper message, projected."""

    message_id: str = Field(description="Beeper message id")
    chat_id: str = Field(description="Chat")
    timestamp: datetime = Field(description="When, UTC")
    sender_id: str = Field(description="Beeper participant id of the sender")
    sender_name: str = Field(description="Sender display name")
    is_sender: bool = Field(description="the user sent it")
    text: str = Field(description="Message text; may be empty for media")


class TextSearchHit(FrozenModel):
    """One search result over texts."""

    chat_id: str = Field(description="Chat")
    message_id: str = Field(description="Message")
    timestamp: datetime = Field(description="When")
    sender_name: str = Field(description="Who")
    is_sender: bool = Field(description="the user sent it")
    network: str = Field(description="Network")
    chat_title: str = Field(description="Chat title")
    snippet: str = Field(description="Excerpt with [ ] around matches")
    sender_handle_key: str = Field(
        default="", description="The sender's handle, so they can be linked"
    )
    other_handle_keys: tuple[str, ...] = Field(
        default=(), description="Everyone else in the chat but the user"
    )


class JudgmentRow(FrozenModel):
    """What the store persists for one judge verdict."""

    person_id: PersonId = Field(description="Whose")
    verdict: str = Field(description="PERSON / GROUP / AUTOMATED / UNSURE / EXCLUDED")
    reason: str = Field(description="One clause")
    judged_at: datetime = Field(description="When")
    model: str = Field(description="Which model")


class MergeJudgmentRow(FrozenModel):
    """The model's call on one possible-duplicate pair, keyed by the handles so it survives rebuilds."""

    pair_key: str = Field(description="Sorted handles of both sides, joined")
    handles_a: tuple[str, ...] = Field(
        description="Handles of the first person at judgment time"
    )
    handles_b: tuple[str, ...] = Field(
        description="Handles of the second person at judgment time"
    )
    name: str = Field(description="The shared name")
    verdict: str = Field(description="same / different / unsure")
    confidence: float = Field(description="0..1")
    reason: str = Field(description="One or two clauses")
    judged_at: datetime = Field(description="When")
    model: str = Field(description="Which model")


# SQLite waits this long for a writer before giving up. The web app keeps the short default so an edit fails fast
# instead of hanging; a batch job (a judge, a rebuild) asks for the long one, because another rebuild may hold the
# lock for several minutes and losing a batch is worse than waiting.
DEFAULT_LOCK_WAIT_SECONDS: Final[int] = 60
BATCH_LOCK_WAIT_SECONDS: Final[int] = 900


class ThreadJudgmentRow(FrozenModel):
    """What one unnamed number's text thread says about who is behind it."""

    handle_key: HandleKey = Field(description="The phone or chat handle")
    kind: str = Field(description="warm / cold / automated / unsure")
    name: str = Field(
        description="The person's name if the thread reveals it, else empty"
    )
    who: str = Field(description="One clause on who they are")
    confidence: float = Field(description="0..1")
    judged_at: datetime = Field(description="When")
    model: str = Field(description="Which model")


@pure
def gmail_sort_key(message_id: str) -> str:
    """Gmail lists messages newest first by id; ids are hex, so padding makes them sort as text."""
    return message_id.rjust(20, "0")


class ArchiveStatus(FrozenModel):
    """Counts and freshness of the archive."""

    message_count_by_account: dict[str, int] = Field(
        description="Stored messages per inbox"
    )
    oldest_message_at: datetime | None = Field(description="Earliest stored message")
    newest_message_at: datetime | None = Field(description="Latest stored message")
    contact_count: int = Field(description="Stored contact cards")
    calendar_event_count: int = Field(description="Stored calendar events")
    raw_record_count: int = Field(description="Raw records of every source")
    text_message_count: int = Field(description="Stored Beeper messages")
    chat_count: int = Field(description="Stored Beeper chats")
    note_count: int = Field(description="Stored notes from the vaults")
    recorded_meeting_count: int = Field(description="Stored recorded meetings")


@pure
def _to_epoch(value: datetime) -> int:
    return int(value.timestamp())


@pure
def _from_epoch(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


@pure
def _participants_json(participants: Sequence[MessageParticipant]) -> str:
    return json.dumps([[p.name, str(p.address)] for p in participants])


@pure
def _participants_from_json(value: str) -> tuple[MessageParticipant, ...]:
    return tuple(
        MessageParticipant(name=name, address=EmailAddress(address))
        for name, address in json.loads(value)
    )


class ArchiveStore(MutableModel):
    """The SQLite archive: raw records, projected messages, contacts, events, and the search index."""

    path: Path = Field(frozen=True, description="Where the SQLite file lives")
    _connection: sqlite3.Connection = PrivateAttr()

    def open(
        self,
        lock_wait_seconds: int = DEFAULT_LOCK_WAIT_SECONDS,
        cache_kib: int = DEFAULT_CACHE_KIB,
    ) -> None:
        """Raises StoreError if the file cannot be opened or the schema cannot be applied.

        `cache_kib` is the page cache for this connection; a long-lived reader such as the CRM app
        asks for more so its pages survive between clicks."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
            for pragma in _PRAGMAS:
                self._connection.execute(pragma)
            self._connection.execute(f"PRAGMA cache_size=-{int(cache_kib)}")
            # Applying the (idempotent) schema takes the write lock for an instant; the wait is set first so a
            # long rebuild elsewhere does not turn that into a failed open.
            self._connection.execute(
                f"PRAGMA busy_timeout={int(lock_wait_seconds) * 1000}"
            )
            self._connection.executescript(_SCHEMA)
        except sqlite3.Error as e:
            raise StoreError(f"Cannot open archive at {self.path}: {e}") from e

    def open_existing(self) -> None:
        """Open an already-migrated archive without requesting SQLite's schema write lock."""
        try:
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
            self._connection.execute("PRAGMA busy_timeout=60000")
            self._connection.execute("PRAGMA temp_store=MEMORY")
            self._connection.execute(f"PRAGMA cache_size=-{DEFAULT_CACHE_KIB}")
        except sqlite3.Error as e:
            raise StoreError(f"Cannot open existing archive at {self.path}: {e}") from e

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    # --- sync state ---

    def get_sync_value(
        self, source: SourceKind, account: AccountEmail, key: str
    ) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM sync_state WHERE source=? AND account=? AND key=?",
            (source.value, str(account), key),
        ).fetchone()
        return None if row is None else str(row[0])

    def set_sync_value(
        self, source: SourceKind, account: AccountEmail, key: str, value: str
    ) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO sync_state (source, account, key, value) VALUES (?, ?, ?, ?)",
            (source.value, str(account), key, value),
        )

    # --- raw records ---

    def upsert_raw_record(
        self,
        source: SourceKind,
        account: AccountEmail,
        record_id: str,
        raw: dict[str, Any],
        fetched_at: datetime,
    ) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO raw_records (source, account, record_id, fetched_at, raw_zlib) VALUES (?, ?, ?, ?, ?)",
            (
                source.value,
                str(account),
                record_id,
                fetched_at.isoformat(),
                zlib.compress(json.dumps(raw).encode()),
            ),
        )

    def get_raw_record(
        self, source: SourceKind, account: AccountEmail, record_id: str
    ) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT raw_zlib FROM raw_records WHERE source=? AND account=? AND record_id=?",
            (source.value, str(account), record_id),
        ).fetchone()
        return None if row is None else json.loads(zlib.decompress(row[0]))

    def existing_record_ids(
        self, source: SourceKind, account: AccountEmail, candidate_ids: AbstractSet[str]
    ) -> set[str]:
        if not candidate_ids:
            return set()
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = self._connection.execute(
            f"SELECT record_id FROM raw_records WHERE source=? AND account=? AND record_id IN ({placeholders})",
            (source.value, str(account), *candidate_ids),
        ).fetchall()
        return {str(row[0]) for row in rows}

    # --- Gmail label state (kept by gmail_sync) ---

    def gmail_label_rows(
        self, account: AccountEmail, message_ids: AbstractSet[str]
    ) -> dict[str, GmailLabelRow]:
        rows: dict[str, GmailLabelRow] = {}
        ids = sorted(message_ids)
        for start in range(0, len(ids), _IN_CLAUSE_CHUNK):
            chunk = ids[start : start + _IN_CLAUSE_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            for row in self._connection.execute(
                "SELECT message_id, thread_id, internal_ms, label_ids_json, is_deleted FROM gmail_label_state "
                f"WHERE account=? AND message_id IN ({placeholders})",
                (str(account), *chunk),
            ):
                rows[str(row[0])] = GmailLabelRow(
                    account=account,
                    message_id=GmailMessageId(row[0]),
                    thread_id=GmailThreadId(row[1]),
                    internal_ms=int(row[2]),
                    label_ids=tuple(json.loads(row[3])),
                    is_deleted=bool(row[4]),
                )
        return rows

    def save_gmail_label_row(
        self, row: GmailLabelRow, change: GmailChangeKind | None, at: datetime
    ) -> None:
        """Write the row; a change kind also appends it to the change feed."""
        labels_json = json.dumps(sorted(set(row.label_ids)))
        is_inbox = 1 if "INBOX" in row.label_ids and not row.is_deleted else 0
        self._connection.execute(
            "INSERT OR REPLACE INTO gmail_label_state (account, message_id, thread_id, sort_key, internal_ms, "
            "label_ids_json, is_inbox, is_deleted, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(row.account),
                str(row.message_id),
                str(row.thread_id),
                gmail_sort_key(str(row.message_id)),
                row.internal_ms,
                labels_json,
                is_inbox,
                1 if row.is_deleted else 0,
                at.isoformat(),
            ),
        )
        if change is not None:
            self._connection.execute(
                "INSERT INTO gmail_changes (account, message_id, thread_id, kind, label_ids_json, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(row.account), str(row.message_id), str(row.thread_id), change.value, labels_json, at.isoformat()),
            )

    def gmail_label_row_ids(
        self, account: AccountEmail, label_id: str, is_inbox_only: bool
    ) -> set[str]:
        """Ids of rows (not deleted) currently carrying the label, optionally only those in the inbox."""
        sql = "SELECT message_id FROM gmail_label_state WHERE account=? AND is_deleted=0 AND label_ids_json LIKE ?"
        if is_inbox_only:
            sql += " AND is_inbox=1"
        rows = self._connection.execute(sql, (str(account), f'%"{label_id}"%')).fetchall()
        return {str(row[0]) for row in rows}

    def gmail_inbox_ids_missing_raw(self, account: AccountEmail, limit: int) -> list[str]:
        """Inbox messages the archive holds no copy of yet, newest first."""
        rows = self._connection.execute(
            "SELECT s.message_id FROM gmail_label_state s LEFT JOIN raw_records r "
            "ON r.source=? AND r.account=s.account AND r.record_id=s.message_id "
            "WHERE s.account=? AND s.is_inbox=1 AND r.record_id IS NULL ORDER BY s.sort_key DESC LIMIT ?",
            (SourceKind.GMAIL.value, str(account), limit),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def stored_gmail_labels(
        self, account: AccountEmail, message_ids: AbstractSet[str]
    ) -> dict[str, tuple[str, int, tuple[str, ...]]]:
        """(thread id, internal ms, labels) as of when each stored message was fetched."""
        found: dict[str, tuple[str, int, tuple[str, ...]]] = {}
        ids = sorted(message_ids)
        for start in range(0, len(ids), _IN_CLAUSE_CHUNK):
            chunk = ids[start : start + _IN_CLAUSE_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            for row in self._connection.execute(
                f"SELECT message_id, thread_id, ts, label_ids_json FROM messages WHERE account=? AND message_id IN ({placeholders})",
                (str(account), *chunk),
            ):
                found[str(row[0])] = (str(row[1]), int(row[2]) * 1000, tuple(json.loads(row[3])))
        return found

    def save_sent_bcc(self, account: AccountEmail, message_id: str, addresses: Sequence[str]) -> None:
        self._connection.executemany(
            "INSERT OR IGNORE INTO gmail_sent_bcc (account, address, message_id) VALUES (?, ?, ?)",
            [(str(account), address, message_id) for address in addresses],
        )

    def unrowed_message_ids_since(self, account: AccountEmail, since: datetime) -> list[str]:
        """Stored messages since then that have no label row yet, newest first."""
        rows = self._connection.execute(
            "SELECT m.message_id FROM messages m LEFT JOIN gmail_label_state s ON s.account=m.account AND s.message_id=m.message_id "
            "WHERE m.ts >= ? AND m.account=? AND s.message_id IS NULL ORDER BY m.ts DESC",
            (_to_epoch(since), str(account)),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def stored_draft_ids(self, account: AccountEmail) -> list[str]:
        """Stored messages that were drafts when fetched: written from one of the user's sending
        addresses, not sent, labelled DRAFT."""
        rows = self._connection.execute(
            "SELECT DISTINCT p.message_id FROM message_participants p WHERE p.account=? AND p.role='from' AND p.direction=? "
            "AND p.address IN (SELECT DISTINCT address FROM message_participants WHERE account=? AND role='from' AND direction=?)",
            (str(account), Direction.RECEIVED.value, str(account), Direction.SENT.value),
        ).fetchall()
        candidates = {str(row[0]) for row in rows}
        labels = self.stored_gmail_labels(account, candidates)
        return sorted(mid for mid, (_thread, _ms, label_ids) in labels.items() if "DRAFT" in label_ids)

    def sent_message_ids(self, account: AccountEmail) -> list[str]:
        """Every stored message the user sent from this mailbox, sorted by id."""
        rows = self._connection.execute(
            "SELECT DISTINCT message_id FROM message_participants WHERE account=? AND direction=?",
            (str(account), Direction.SENT.value),
        ).fetchall()
        return sorted(str(row[0]) for row in rows)

    def raw_records(
        self, source: SourceKind, account: AccountEmail, record_ids: AbstractSet[str]
    ) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        ids = sorted(record_ids)
        for start in range(0, len(ids), _IN_CLAUSE_CHUNK):
            chunk = ids[start : start + _IN_CLAUSE_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            for record_id, blob in self._connection.execute(
                f"SELECT record_id, raw_zlib FROM raw_records WHERE source=? AND account=? AND record_id IN ({placeholders})",
                (source.value, str(account), *chunk),
            ):
                found[str(record_id)] = json.loads(zlib.decompress(blob))
        return found

    def prune_gmail_changes(self, older_than: datetime) -> int:
        cursor = self._connection.execute("DELETE FROM gmail_changes WHERE at < ?", (older_than.isoformat(),))
        return int(cursor.rowcount)

    # --- messages ---

    def upsert_message(self, message: ParsedEmail) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO messages (account, message_id, thread_id, ts, direction, from_name, from_addr, "
            "to_json, cc_json, subject, snippet, text, label_ids_json, is_automated, rfc_message_id, in_reply_to) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(message.account),
                str(message.message_id),
                str(message.thread_id),
                _to_epoch(message.timestamp),
                message.direction.value,
                message.sender.name if message.sender else "",
                str(message.sender.address) if message.sender else "",
                _participants_json(message.to_recipients),
                _participants_json(message.cc_recipients),
                message.subject,
                message.snippet,
                message.text,
                json.dumps(list(message.label_ids)),
                1 if message.is_automated else 0,
                message.rfc_message_id,
                message.in_reply_to,
            ),
        )
        self._connection.execute(
            "DELETE FROM message_participants WHERE account=? AND message_id=?",
            (str(message.account), str(message.message_id)),
        )
        participant_rows = (
            [("from", p) for p in ([message.sender] if message.sender else [])]
            + [("to", p) for p in message.to_recipients]
            + [("cc", p) for p in message.cc_recipients]
        )
        self._connection.executemany(
            "INSERT OR REPLACE INTO message_participants (account, message_id, role, address, name, ts, direction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    str(message.account),
                    str(message.message_id),
                    role,
                    str(p.address),
                    p.name,
                    _to_epoch(message.timestamp),
                    message.direction.value,
                )
                for role, p in participant_rows
            ],
        )

    def iter_messages(self) -> Iterator[ParsedEmail]:
        cursor = self._connection.execute(
            "SELECT account, message_id, thread_id, ts, direction, from_name, from_addr, to_json, cc_json, subject, "
            "snippet, text, label_ids_json, is_automated, rfc_message_id, in_reply_to FROM messages ORDER BY ts"
        )
        for row in cursor:
            yield _message_from_row(row)

    def get_message(
        self, account: AccountEmail, message_id: GmailMessageId
    ) -> ParsedEmail | None:
        row = self._connection.execute(
            "SELECT account, message_id, thread_id, ts, direction, from_name, from_addr, to_json, cc_json, subject, "
            "snippet, text, label_ids_json, is_automated, rfc_message_id, in_reply_to FROM messages "
            "WHERE account=? AND message_id=?",
            (str(account), str(message_id)),
        ).fetchone()
        return None if row is None else _message_from_row(row)

    def search_messages(self, query: str, limit: int) -> list[MessageSearchHit]:
        """The newest `limit` of the best-matching messages.

        A common word matches tens of thousands of messages; sorting all of them by date meant
        reading every one from disk. The search index ranks its matches on its own, so the
        candidates are its top SEARCH_CANDIDATES; those are dated (a cheap read of each row's
        head), and only the `limit` newest have their bodies read for the snippet."""
        rows = self._connection.execute(
            "SELECT m.account, m.message_id, m.thread_id, m.ts, m.direction, m.from_name, m.from_addr, m.subject, "
            "snippet(messages_fts, 1, '[', ']', ' … ', 24), m.to_json, m.cc_json "
            "FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid "
            "WHERE messages_fts.rowid IN ("
            "    SELECT dated.rowid FROM messages dated WHERE dated.rowid IN ("
            "        SELECT rowid FROM messages_fts WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?)"
            "    ORDER BY dated.ts DESC LIMIT ?) "
            "AND messages_fts MATCH ? ORDER BY m.ts DESC",
            (query, SEARCH_CANDIDATES, limit, query),
        ).fetchall()
        return [
            MessageSearchHit(
                account=AccountEmail(row[0]),
                message_id=GmailMessageId(row[1]),
                thread_id=GmailThreadId(row[2]),
                timestamp=_from_epoch(row[3]),
                direction=Direction(row[4]),
                from_name=row[5],
                from_addr=row[6],
                subject=row[7],
                snippet=row[8],
                recipient_addresses=tuple(
                    str(address)
                    for _name, address in [*json.loads(row[9]), *json.loads(row[10])]
                ),
            )
            for row in rows
        ]

    def search_events(self, query: str, limit: int) -> list[EventSearchHit]:
        rows = self._connection.execute(
            "SELECT e.account, e.event_id, e.start_ts, e.summary, e.attendees_json "
            "FROM calendar_events_fts JOIN calendar_events e ON e.rowid = calendar_events_fts.rowid "
            "WHERE calendar_events_fts MATCH ? ORDER BY e.start_ts DESC LIMIT ?",
            (query, limit),
        ).fetchall()
        return [
            EventSearchHit(
                account=AccountEmail(row[0]),
                event_id=CalendarEventId(row[1]),
                start=_from_epoch(row[2]),
                summary=row[3],
                attendee_count=len(json.loads(row[4])),
                attendee_addresses=tuple(
                    str(attendee[0]) for attendee in json.loads(row[4])
                ),
            )
            for row in rows
        ]

    def rebuild_search_indexes(self) -> None:
        """Re-derive both full-text indexes from their tables (after a schema change or a suspected drift)."""
        self._connection.execute(
            "INSERT INTO messages_fts (messages_fts) VALUES ('rebuild')"
        )
        self._connection.execute(
            "INSERT INTO calendar_events_fts (calendar_events_fts) VALUES ('rebuild')"
        )
        self._connection.execute(
            "INSERT INTO beeper_messages_fts (beeper_messages_fts) VALUES ('rebuild')"
        )
        self._connection.execute("INSERT INTO notes_fts (notes_fts) VALUES ('rebuild')")
        self._connection.execute(
            "INSERT INTO meetings_fts (meetings_fts) VALUES ('rebuild')"
        )
        self._connection.commit()

    # --- contacts ---

    def upsert_contact(self, card: ContactCard) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO contacts (account, resource_name, display_name, emails_json, phones_json, organization, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(card.account),
                str(card.resource_name),
                card.display_name,
                json.dumps([str(e) for e in card.emails]),
                json.dumps([str(p) for p in card.phones]),
                card.organization,
                card.title,
            ),
        )

    def iter_contacts(self) -> Iterator[ContactCard]:
        cursor = self._connection.execute(
            "SELECT account, resource_name, display_name, emails_json, phones_json, organization, title FROM contacts"
        )
        for row in cursor:
            yield ContactCard(
                account=AccountEmail(row[0]),
                resource_name=ContactResourceName(row[1]),
                display_name=row[2],
                emails=tuple(EmailAddress(e) for e in json.loads(row[3])),
                phones=tuple(PhoneNumber(p) for p in json.loads(row[4])),
                organization=row[5],
                title=row[6],
            )

    # --- calendar ---

    def upsert_calendar_event(self, event: CalendarEvent) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO calendar_events (account, event_id, series_id, summary, start_ts, end_ts, organizer, attendees_json, is_organized_by_owner) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(event.account),
                str(event.event_id),
                str(event.series_id),
                event.summary,
                _to_epoch(event.start),
                _to_epoch(event.end),
                str(event.organizer) if event.organizer else "",
                json.dumps(
                    [
                        [str(a.address), a.name, a.response_status, a.is_self]
                        for a in event.attendees
                    ]
                ),
                1 if event.is_organized_by_owner else 0,
            ),
        )

    def iter_calendar_events(self) -> Iterator[CalendarEvent]:
        cursor = self._connection.execute(
            "SELECT account, event_id, series_id, summary, start_ts, end_ts, organizer, attendees_json, is_organized_by_owner "
            "FROM calendar_events ORDER BY start_ts"
        )
        for row in cursor:
            yield CalendarEvent(
                account=AccountEmail(row[0]),
                event_id=CalendarEventId(row[1]),
                series_id=CalendarEventId(row[2]),
                summary=row[3],
                start=_from_epoch(row[4]),
                end=_from_epoch(row[5]),
                organizer=EmailAddress(row[6]) if row[6] else None,
                attendees=tuple(
                    EventAttendee(
                        address=EmailAddress(a[0]),
                        name=a[1],
                        response_status=a[2],
                        is_self=bool(a[3]),
                    )
                    for a in json.loads(row[7])
                ),
                is_organized_by_owner=bool(row[8]),
            )

    # --- people (derived; rebuilt by the identity pass, ids kept stable through person_handles) ---

    def person_id_by_handle(self) -> dict[HandleKey, PersonId]:
        rows = self._connection.execute(
            "SELECT handle_key, person_id FROM person_handles"
        ).fetchall()
        return {HandleKey(row[0]): PersonId(row[1]) for row in rows}

    def replace_people(
        self,
        people: Sequence["ResolvedPersonRow"],
        merge_candidates: Sequence[tuple[PersonId, PersonId, str]],
    ) -> None:
        """Rewrite the derived people tables in one transaction."""
        with self.transaction():
            self._connection.execute("DELETE FROM persons")
            self._connection.execute("DELETE FROM person_handles")
            self._connection.execute("DELETE FROM merge_candidates")
            self._connection.executemany(
                "INSERT OR REPLACE INTO merge_candidates (person_id_a, person_id_b, reason) VALUES (?, ?, ?)",
                [(str(a), str(b), reason) for a, b, reason in merge_candidates],
            )
            self._connection.executemany(
                "INSERT INTO persons (person_id, canonical_name, aliases_json, emails_json, phones_json, organization, title, "
                "tier, is_human, is_colleague, hand_kept_category, score, received_human_count, received_automated_count, sent_count, meeting_count, "
                "text_days_sent, text_days_received, first_contact_ts, last_contact_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(p.person_id),
                        p.canonical_name,
                        json.dumps(list(p.aliases)),
                        json.dumps(list(p.emails)),
                        json.dumps(list(p.phones)),
                        p.organization,
                        p.title,
                        p.tier,
                        1 if p.is_human else 0,
                        1 if p.is_colleague else 0,
                        p.hand_kept_category,
                        p.score,
                        p.received_human_count,
                        p.received_automated_count,
                        p.sent_count,
                        p.meeting_count,
                        p.text_days_sent,
                        p.text_days_received,
                        _to_epoch(p.first_contact) if p.first_contact else None,
                        _to_epoch(p.last_contact) if p.last_contact else None,
                    )
                    for p in people
                ],
            )
            self._connection.executemany(
                "INSERT INTO person_handles (handle_key, person_id) VALUES (?, ?)",
                [(str(key), str(p.person_id)) for p in people for key in p.handle_keys],
            )
            self._apply_edits_to_persons()
            self._rebuild_person_months()

    def _rebuild_person_months(self) -> None:
        """Per person and month: human emails, meetings, and days with texts -- what the activity grids draw from."""
        person_by_email: dict[str, str] = {}
        for handle_key, person_id in self._connection.execute(
            "SELECT handle_key, person_id FROM person_handles WHERE handle_key LIKE 'email:%'"
        ):
            person_by_email[str(handle_key)[len("email:") :]] = str(person_id)
        counts: dict[tuple[str, str], list[int]] = {}

        def bump(person_id: str, month: str, slot: int, amount: int) -> None:
            counts.setdefault((person_id, month), [0, 0, 0])[slot] += amount

        for address, month, count in self._connection.execute(
            "SELECT p.address, strftime('%Y-%m', m.ts, 'unixepoch'), COUNT(*) FROM message_participants p "
            "JOIN messages m ON m.account = p.account AND m.message_id = p.message_id WHERE m.is_automated = 0 GROUP BY 1, 2"
        ):
            person_id = person_by_email.get(str(address))
            if person_id is not None:
                bump(person_id, str(month), 0, int(count))
        for start_ts, attendees_json in self._connection.execute(
            "SELECT start_ts, attendees_json FROM calendar_events"
        ):
            month = datetime.fromtimestamp(int(start_ts), tz=timezone.utc).strftime(
                "%Y-%m"
            )
            for attendee in json.loads(attendees_json):
                person_id = person_by_email.get(str(attendee[0]))
                if person_id is not None:
                    bump(person_id, month, 1, 1)
        for person_id, month, days in self._connection.execute(
            "SELECT h.person_id, strftime('%Y-%m', m.ts, 'unixepoch'), COUNT(DISTINCT m.ts / 86400) FROM person_handles h "
            "JOIN beeper_participants p ON p.handle_key = h.handle_key AND p.is_self = 0 "
            "JOIN beeper_messages m ON m.chat_id = p.chat_id GROUP BY 1, 2"
        ):
            bump(str(person_id), str(month), 2, int(days))
        self._connection.execute("DELETE FROM person_months")
        self._connection.executemany(
            "INSERT INTO person_months (person_id, month, emails, meetings, text_days) VALUES (?, ?, ?, ?, ?)",
            [
                (person_id, month, emails, meetings, text_days)
                for (person_id, month), (emails, meetings, text_days) in counts.items()
            ],
        )

    def rebuild_person_months(self) -> None:
        """Recount the monthly activity table on its own (the identity pass does it as part of replace_people)."""
        with self.transaction():
            self._rebuild_person_months()

    # --- the user's in-place edits: the user's word beats the model's and the sources' ---

    def set_person_edit(self, person_id: PersonId, field: str, value: str) -> None:
        """Record what the user typed for one field; an empty value withdraws the edit and puts back what was there."""
        if field not in EDITABLE_FIELDS:
            raise StoreError(f"Not an editable field: {field!r}")
        cleaned = (
            ", ".join(parse_tag_list(value))
            if field in ("interests", "background")
            else value.strip()
        )
        column = PERSON_COLUMN_BY_FIELD.get(field)
        with self.transaction():
            existing = self._connection.execute(
                "SELECT original FROM person_edits WHERE person_id = ? AND field = ?",
                (str(person_id), field),
            ).fetchone()
            if cleaned:
                if existing is not None:
                    original = str(existing[0])
                elif column is not None:
                    row = self._connection.execute(
                        f"SELECT {column} FROM persons WHERE person_id = ?",
                        (str(person_id),),
                    ).fetchone()
                    original = str(row[0]) if row is not None else ""
                else:
                    original = ""
                self._connection.execute(
                    "INSERT OR REPLACE INTO person_edits (person_id, field, value, original, edited_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        str(person_id),
                        field,
                        cleaned,
                        original,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            else:
                self._connection.execute(
                    "DELETE FROM person_edits WHERE person_id = ? AND field = ?",
                    (str(person_id), field),
                )
                if existing is not None and column is not None:
                    self._connection.execute(
                        f"UPDATE persons SET {column} = ? WHERE person_id = ?",
                        (str(existing[0]), str(person_id)),
                    )
            self._apply_edits_to_persons()

    def person_edits(self, person_id: PersonId) -> dict[str, str]:
        rows = self._connection.execute(
            "SELECT field, value FROM person_edits WHERE person_id = ?",
            (str(person_id),),
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def _apply_edits_to_persons(self) -> None:
        """Copy the edited header fields onto the derived persons rows (the rows are rebuilt by every identity pass)."""
        for field, column in PERSON_COLUMN_BY_FIELD.items():
            self._connection.execute(
                f"UPDATE persons SET {column} = (SELECT value FROM person_edits e WHERE e.person_id = persons.person_id AND e.field = ?) "
                "WHERE person_id IN (SELECT person_id FROM person_edits WHERE field = ?)",
                (field, field),
            )

    # --- outreach lists: the user's include / exclude decisions per list ---

    def set_list_decision(
        self, list_name: str, person_id: PersonId, decision: str
    ) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT OR REPLACE INTO list_decisions (list_name, person_id, decision, decided_at) VALUES (?, ?, ?, ?)",
                (
                    list_name,
                    str(person_id),
                    decision,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def list_decisions(self, list_name: str) -> dict[PersonId, str]:
        rows = self._connection.execute(
            "SELECT person_id, decision FROM list_decisions WHERE list_name = ?",
            (list_name,),
        ).fetchall()
        return {PersonId(row[0]): str(row[1]) for row in rows}

    # --- cheap tags (interests, background, reach, fit) ---

    def save_person_tags(self, rows: Sequence[PersonTagsRow]) -> None:
        with self.transaction():
            self._connection.executemany(
                "INSERT OR REPLACE INTO person_tags (person_id, interests_json, background_json, reach, reach_why, fit, fit_why, tagged_at, model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(r.person_id),
                        r.interests_json,
                        r.background_json,
                        r.reach,
                        r.reach_why,
                        r.fit,
                        r.fit_why,
                        r.tagged_at.isoformat(),
                        r.model,
                    )
                    for r in rows
                ],
            )

    # --- public social profiles and raw enrichment sources ---

    def save_social_source(
        self,
        *,
        source_id: str,
        person_id: PersonId,
        platform: str,
        source_url: str,
        provider: str,
        fetched_at: datetime,
        status: str,
        raw: Any,
    ) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT OR REPLACE INTO social_profile_sources "
                "(source_id, person_id, platform, source_url, provider, fetched_at, status, raw_zlib) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    str(person_id),
                    platform,
                    source_url,
                    provider,
                    fetched_at.isoformat(),
                    status,
                    zlib.compress(json.dumps(raw, ensure_ascii=False).encode()),
                ),
            )

    def get_social_source(self, source_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT person_id, platform, source_url, provider, fetched_at, status, raw_zlib "
            "FROM social_profile_sources WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "source_id": source_id,
            "person_id": str(row[0]),
            "platform": str(row[1]),
            "source_url": str(row[2]),
            "provider": str(row[3]),
            "fetched_at": str(row[4]),
            "status": str(row[5]),
            "raw": json.loads(zlib.decompress(row[6])),
        }

    def set_social_source_status(self, source_id: str, status: str) -> None:
        with self.transaction():
            self._connection.execute(
                "UPDATE social_profile_sources SET status=? WHERE source_id=?",
                (status, source_id),
            )

    def upsert_social_candidate(
        self,
        *,
        person_id: PersonId,
        platform: str,
        profile_url: str,
        username: str,
        discovered_via: str,
        confidence: float,
        reason: str,
        state: str,
        seen_at: datetime,
    ) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO social_profile_candidates "
                "(person_id, platform, profile_url, username, discovered_via, confidence, reason, state, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(person_id, platform, profile_url) DO UPDATE SET "
                "username=excluded.username, discovered_via=excluded.discovered_via, "
                "confidence=MAX(social_profile_candidates.confidence, excluded.confidence), "
                "reason=excluded.reason, state=CASE WHEN social_profile_candidates.state='accepted' THEN 'accepted' ELSE excluded.state END, "
                "last_seen_at=excluded.last_seen_at",
                (
                    str(person_id),
                    platform,
                    profile_url,
                    username,
                    discovered_via,
                    confidence,
                    reason,
                    state,
                    seen_at.isoformat(),
                    seen_at.isoformat(),
                ),
            )

    def save_social_profile(self, row: SocialProfileRow) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO social_profiles "
                "(person_id, platform, profile_url, username, display_name, follower_count, following_count, connection_count, "
                "biography, headline, is_verified, confidence, match_reason, discovered_via, observed_at, source_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(person_id, platform) DO UPDATE SET "
                "profile_url=excluded.profile_url, username=excluded.username, display_name=excluded.display_name, "
                "follower_count=COALESCE(excluded.follower_count, social_profiles.follower_count), "
                "following_count=COALESCE(excluded.following_count, social_profiles.following_count), "
                "connection_count=COALESCE(excluded.connection_count, social_profiles.connection_count), "
                "biography=CASE WHEN excluded.biography!='' THEN excluded.biography ELSE social_profiles.biography END, "
                "headline=CASE WHEN excluded.headline!='' THEN excluded.headline ELSE social_profiles.headline END, "
                "is_verified=COALESCE(excluded.is_verified, social_profiles.is_verified), "
                "confidence=MAX(social_profiles.confidence, excluded.confidence), match_reason=excluded.match_reason, "
                "discovered_via=excluded.discovered_via, observed_at=excluded.observed_at, "
                "source_id=CASE WHEN excluded.source_id!='' THEN excluded.source_id ELSE social_profiles.source_id END "
                "WHERE excluded.observed_at >= social_profiles.observed_at",
                (
                    str(row.person_id),
                    row.platform,
                    row.profile_url,
                    row.username,
                    row.display_name,
                    row.follower_count,
                    row.following_count,
                    row.connection_count,
                    row.biography,
                    row.headline,
                    None if row.is_verified is None else int(row.is_verified),
                    row.confidence,
                    row.match_reason,
                    row.discovered_via,
                    row.observed_at.isoformat(),
                    row.source_id,
                ),
            )
            self._connection.execute(
                "UPDATE social_profile_candidates SET state='accepted', last_seen_at=? "
                "WHERE person_id=? AND platform=? AND profile_url=?",
                (
                    row.observed_at.isoformat(),
                    str(row.person_id),
                    row.platform,
                    row.profile_url,
                ),
            )

    def social_profiles(self, person_id: PersonId) -> tuple[SocialProfileRow, ...]:
        rows = self._connection.execute(
            "SELECT person_id, platform, profile_url, username, display_name, follower_count, following_count, connection_count, "
            "biography, headline, is_verified, confidence, match_reason, discovered_via, observed_at, source_id "
            "FROM social_profiles WHERE person_id=? ORDER BY CASE platform WHEN 'linkedin' THEN 1 WHEN 'x' THEN 2 ELSE 3 END",
            (str(person_id),),
        ).fetchall()
        return tuple(
            SocialProfileRow(
                person_id=PersonId(str(row[0])),
                platform=str(row[1]),
                profile_url=str(row[2]),
                username=str(row[3]),
                display_name=str(row[4]),
                follower_count=None if row[5] is None else int(row[5]),
                following_count=None if row[6] is None else int(row[6]),
                connection_count=None if row[7] is None else int(row[7]),
                biography=str(row[8]),
                headline=str(row[9]),
                is_verified=None if row[10] is None else bool(row[10]),
                confidence=float(row[11]),
                match_reason=str(row[12]),
                discovered_via=str(row[13]),
                observed_at=datetime.fromisoformat(str(row[14])),
                source_id=str(row[15]),
            )
            for row in rows
        )

    def set_social_enrichment_metadata(self, key: str, value: str) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT OR REPLACE INTO social_enrichment_metadata (key, value) VALUES (?, ?)",
                (key, value),
            )

    def social_enrichment_metadata(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM social_enrichment_metadata WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    def reset_email_signature_social_candidates(self) -> None:
        """Rebuild inferred signature candidates without discarding enriched profiles."""
        with self.transaction():
            self._connection.execute(
                "DELETE FROM social_profile_candidates WHERE discovered_via='email_signature'"
            )
            self._connection.execute(
                "UPDATE social_enrichment_queue SET state='pending', last_error='', updated_at=? "
                "WHERE person_id NOT IN (SELECT person_id FROM social_profiles WHERE follower_count IS NOT NULL)",
                (datetime.now(timezone.utc).isoformat(),),
            )

    def refresh_social_enrichment_queue(self, now: datetime) -> int:
        """Add current humans and refresh their priority without resetting completed work."""
        before = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM social_enrichment_queue"
            ).fetchone()[0]
        )
        with self.transaction():
            self._connection.execute(
                "INSERT INTO social_enrichment_queue "
                "(person_id, tier, score, state, attempts, next_attempt_at, last_error, updated_at) "
                "SELECT person_id, tier, score, 'pending', 0, '', '', ? FROM persons "
                "WHERE is_human=1 "
                "ON CONFLICT(person_id) DO UPDATE SET tier=excluded.tier, score=excluded.score",
                (now.isoformat(),),
            )
        after = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM social_enrichment_queue"
            ).fetchone()[0]
        )
        return after - before

    def pending_social_fetches(
        self, limit: int
    ) -> tuple[tuple[str, str, str, str, str, float, str], ...]:
        recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        rows = self._connection.execute(
            "SELECT s.person_id, s.platform, s.profile_url, s.username, s.discovered_via, s.confidence, s.match_reason "
            "FROM social_profiles s JOIN persons p ON p.person_id=s.person_id "
            "WHERE p.is_human=1 "
            "AND NOT EXISTS (SELECT 1 FROM social_profile_sources src "
            "WHERE src.person_id=s.person_id AND src.platform=s.platform "
            "AND (lower(src.source_url)=lower(s.profile_url) OR src.source_id=s.source_id) "
            "AND src.provider='brightdata' "
            "AND src.status!='rejected' AND src.fetched_at>=?) "
            "ORDER BY p.tier ASC, p.score DESC, CASE s.platform WHEN 'x' THEN 1 WHEN 'instagram' THEN 2 ELSE 3 END LIMIT ?",
            (recent_cutoff, limit),
        ).fetchall()
        return tuple(
            (
                str(r[0]),
                str(r[1]),
                str(r[2]),
                str(r[3]),
                str(r[4]),
                float(r[5]),
                str(r[6]),
            )
            for r in rows
        )

    def set_social_queue_state(
        self, person_id: PersonId, state: str, error: str = ""
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction():
            self._connection.execute(
                "UPDATE social_enrichment_queue SET state=?, attempts=attempts+1, last_error=?, updated_at=? WHERE person_id=?",
                (state, error[:500], now, str(person_id)),
            )

    def pending_social_discoveries(self, limit: int) -> tuple[tuple[Any, ...], ...]:
        """Return people not yet searched publicly, ordered by relationship tier and score."""
        rows = self._connection.execute(
            "SELECT p.person_id, p.canonical_name, p.aliases_json, p.emails_json, "
            "p.organization, p.title, p.tier, p.score "
            "FROM social_enrichment_queue q JOIN persons p ON p.person_id=q.person_id "
            "LEFT JOIN social_discovery_status d ON d.person_id=p.person_id "
            "WHERE p.is_human=1 AND (d.person_id IS NULL OR d.state='retry') "
            "ORDER BY p.tier ASC, p.score DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(tuple(row) for row in rows)

    def save_social_discovery_status(
        self,
        person_id: PersonId,
        *,
        state: str,
        profiles_found: int,
        model: str,
        reason: str,
        searched_at: datetime,
    ) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO social_discovery_status "
                "(person_id, state, attempts, profiles_found, model, reason, searched_at) "
                "VALUES (?, ?, 1, ?, ?, ?, ?) "
                "ON CONFLICT(person_id) DO UPDATE SET state=excluded.state, attempts=attempts+1, "
                "profiles_found=excluded.profiles_found, model=excluded.model, "
                "reason=excluded.reason, searched_at=excluded.searched_at",
                (
                    str(person_id),
                    state,
                    profiles_found,
                    model,
                    reason,
                    searched_at.isoformat(),
                ),
            )

    def social_enrichment_status(self) -> dict[str, Any]:
        queued = {
            str(state): int(count)
            for state, count in self._connection.execute(
                "SELECT state, COUNT(*) FROM social_enrichment_queue GROUP BY state"
            ).fetchall()
        }
        profiles = {
            str(platform): int(count)
            for platform, count in self._connection.execute(
                "SELECT platform, COUNT(*) FROM social_profiles GROUP BY platform"
            ).fetchall()
        }
        with_counts = {
            str(platform): int(count)
            for platform, count in self._connection.execute(
                "SELECT platform, COUNT(*) FROM social_profiles WHERE follower_count IS NOT NULL GROUP BY platform"
            ).fetchall()
        }
        return {
            "queue": queued,
            "profiles": profiles,
            "with_follower_counts": with_counts,
        }

    # --- model-written dossier sections ---

    def save_synthesis(self, row: SynthesisRow) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT OR REPLACE INTO person_synthesis (person_id, model, synthesized_at, excerpt_count, one_liner, key_facts, how_we_met, "
                "what_they_care_about, where_things_stand, watch_out, interests_json, background_json, next_steps, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(row.person_id),
                    row.model,
                    row.synthesized_at.isoformat(),
                    row.excerpt_count,
                    row.one_liner,
                    row.key_facts,
                    row.how_we_met,
                    row.what_they_care_about,
                    row.where_things_stand,
                    row.watch_out,
                    row.interests_json,
                    row.background_json,
                    row.next_steps,
                    row.cost_usd,
                ),
            )

    def get_synthesis(self, person_id: PersonId) -> SynthesisRow | None:
        row = self._connection.execute(
            "SELECT person_id, model, synthesized_at, excerpt_count, one_liner, key_facts, how_we_met, what_they_care_about, where_things_stand, "
            "watch_out, interests_json, background_json, next_steps, cost_usd FROM person_synthesis WHERE person_id = ?",
            (str(person_id),),
        ).fetchone()
        if row is None:
            return None
        return SynthesisRow(
            person_id=PersonId(row[0]),
            model=str(row[1]),
            synthesized_at=datetime.fromisoformat(row[2]),
            excerpt_count=int(row[3]),
            one_liner=str(row[4]),
            key_facts=str(row[5]),
            how_we_met=str(row[6]),
            what_they_care_about=str(row[7]),
            where_things_stand=str(row[8]),
            watch_out=str(row[9]),
            interests_json=str(row[10]),
            background_json=str(row[11]),
            next_steps=str(row[12]),
            cost_usd=float(row[13]),
        )

    # --- recorded meetings ---

    def upsert_meeting(self, meeting: MeetingRow) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO meetings (meeting_id, title, started_ts, summary, notes, transcript, calendar_event_id, attendee_emails_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                meeting.meeting_id,
                meeting.title,
                _to_epoch(meeting.started),
                meeting.summary,
                meeting.notes,
                meeting.transcript,
                meeting.calendar_event_id,
                json.dumps(list(meeting.attendee_emails)),
            ),
        )

    def search_meetings(self, query: str, limit: int) -> list[MeetingSearchHit]:
        rows = self._connection.execute(
            "SELECT m.meeting_id, m.title, m.started_ts, snippet(meetings_fts, 2, '[', ']', ' … ', 24) "
            "FROM meetings_fts JOIN meetings m ON m.rowid = meetings_fts.rowid WHERE meetings_fts MATCH ? ORDER BY m.started_ts DESC LIMIT ?",
            (query, limit),
        ).fetchall()
        return [
            MeetingSearchHit(
                meeting_id=str(r[0]),
                title=str(r[1]),
                started=_from_epoch(r[2]),
                snippet=str(r[3]),
            )
            for r in rows
        ]

    # --- notes (Obsidian / Logseq, read-only) ---

    def upsert_note(self, note: NoteRow) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO notes (source, path, title, is_journal, note_date_ts, text, mentions_json, tags_json, properties_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                note.source,
                note.path,
                note.title,
                1 if note.is_journal else 0,
                _to_epoch(note.note_date) if note.note_date else None,
                note.text,
                json.dumps(list(note.mentions)),
                json.dumps(list(note.tags)),
                json.dumps(note.properties),
            ),
        )
        self._connection.execute(
            "DELETE FROM note_mentions WHERE source = ? AND path = ?",
            (note.source, note.path),
        )
        self._connection.executemany(
            "INSERT OR REPLACE INTO note_mentions (source, path, handle_key) VALUES (?, ?, ?)",
            [
                (note.source, note.path, f"note:{' '.join(name.split()).lower()}")
                for name in note.mentions
            ],
        )

    def search_notes(self, query: str, limit: int) -> list[NoteSearchHit]:
        rows = self._connection.execute(
            "SELECT n.source, n.path, n.title, n.note_date_ts, snippet(notes_fts, 1, '[', ']', ' … ', 24) "
            "FROM notes_fts JOIN notes n ON n.rowid = notes_fts.rowid WHERE notes_fts MATCH ? "
            "ORDER BY COALESCE(n.note_date_ts, 0) DESC LIMIT ?",
            (query, limit),
        ).fetchall()
        return [
            NoteSearchHit(
                source=str(r[0]),
                path=str(r[1]),
                title=str(r[2]),
                note_date=_from_epoch(r[3]) if r[3] is not None else None,
                snippet=str(r[4]),
            )
            for r in rows
        ]

    # --- Beeper (texts) ---

    def upsert_beeper_chat(
        self, chat: "BeeperChatRow", participants: Sequence["BeeperParticipantRow"]
    ) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO beeper_chats (chat_id, account_id, network, chat_type, title, participants_json, last_activity_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                chat.chat_id,
                chat.account_id,
                chat.network,
                chat.chat_type,
                chat.title,
                chat.participants_json,
                _to_epoch(chat.last_activity) if chat.last_activity else None,
            ),
        )
        self._connection.execute(
            "DELETE FROM beeper_participants WHERE chat_id = ?", (chat.chat_id,)
        )
        self._connection.executemany(
            "INSERT OR REPLACE INTO beeper_participants (chat_id, participant_id, handle_key, full_name, phone, username, is_self) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    p.chat_id,
                    p.participant_id,
                    str(p.handle_key),
                    p.full_name,
                    p.phone,
                    p.username,
                    1 if p.is_self else 0,
                )
                for p in participants
            ],
        )

    def upsert_beeper_messages(self, messages: Sequence["BeeperMessageRow"]) -> None:
        self._connection.executemany(
            "INSERT OR REPLACE INTO beeper_messages (message_id, chat_id, ts, sender_id, sender_name, is_sender, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    m.message_id,
                    m.chat_id,
                    _to_epoch(m.timestamp),
                    m.sender_id,
                    m.sender_name,
                    1 if m.is_sender else 0,
                    m.text,
                )
                for m in messages
            ],
        )

    def get_beeper_chat_sync(self, chat_id: str) -> tuple[str, bool] | None:
        row = self._connection.execute(
            "SELECT newest_sort_key, is_history_complete FROM beeper_chat_sync WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return None if row is None else (str(row[0]), bool(row[1]))

    def set_beeper_chat_sync(
        self, chat_id: str, newest_sort_key: str, is_history_complete: bool
    ) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO beeper_chat_sync (chat_id, newest_sort_key, is_history_complete) VALUES (?, ?, ?)",
            (chat_id, newest_sort_key, 1 if is_history_complete else 0),
        )

    def search_texts(self, query: str, limit: int) -> list["TextSearchHit"]:
        rows = self._connection.execute(
            "SELECT m.chat_id, m.message_id, m.ts, m.sender_name, m.is_sender, c.network, c.title, "
            "snippet(beeper_messages_fts, 0, '[', ']', ' … ', 24), "
            "COALESCE((SELECT s.handle_key FROM beeper_participants s WHERE s.chat_id = m.chat_id AND s.participant_id = m.sender_id), ''), "
            "COALESCE((SELECT group_concat(o.handle_key, ' ') FROM beeper_participants o WHERE o.chat_id = m.chat_id AND o.is_self = 0), '') "
            "FROM beeper_messages_fts JOIN beeper_messages m ON m.rowid = beeper_messages_fts.rowid "
            "JOIN beeper_chats c ON c.chat_id = m.chat_id "
            "WHERE beeper_messages_fts MATCH ? ORDER BY m.ts DESC LIMIT ?",
            (query, limit),
        ).fetchall()
        return [
            TextSearchHit(
                chat_id=str(r[0]),
                message_id=str(r[1]),
                timestamp=_from_epoch(r[2]),
                sender_name=str(r[3]),
                is_sender=bool(r[4]),
                network=str(r[5]),
                chat_title=str(r[6]),
                snippet=str(r[7]),
                sender_handle_key=str(r[8]),
                other_handle_keys=tuple(str(r[9]).split()),
            )
            for r in rows
        ]

    # --- judge verdicts (survive rebuilds: keyed by the stable person id) ---

    def save_judgments(self, judgments: Sequence["JudgmentRow"]) -> None:
        with self.transaction():
            self._connection.executemany(
                "INSERT OR REPLACE INTO person_judgments (person_id, verdict, reason, judged_at, model) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        str(j.person_id),
                        j.verdict,
                        j.reason,
                        j.judged_at.isoformat(),
                        j.model,
                    )
                    for j in judgments
                ],
            )

    def save_merge_judgments(self, judgments: Sequence["MergeJudgmentRow"]) -> None:
        with self.transaction():
            self._connection.executemany(
                "INSERT OR REPLACE INTO merge_judgments (pair_key, handles_a_json, handles_b_json, name, verdict, confidence, reason, judged_at, model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        j.pair_key,
                        json.dumps(list(j.handles_a)),
                        json.dumps(list(j.handles_b)),
                        j.name,
                        j.verdict,
                        j.confidence,
                        j.reason,
                        j.judged_at.isoformat(),
                        j.model,
                    )
                    for j in judgments
                ],
            )

    def merge_judgments(self) -> list["MergeJudgmentRow"]:
        rows = self._connection.execute(
            "SELECT pair_key, handles_a_json, handles_b_json, name, verdict, confidence, reason, judged_at, model FROM merge_judgments"
        ).fetchall()
        return [
            MergeJudgmentRow(
                pair_key=str(r[0]),
                handles_a=tuple(json.loads(r[1])),
                handles_b=tuple(json.loads(r[2])),
                name=str(r[3]),
                verdict=str(r[4]),
                confidence=float(r[5]),
                reason=str(r[6]),
                judged_at=datetime.fromisoformat(str(r[7])),
                model=str(r[8]),
            )
            for r in rows
        ]

    def save_thread_judgments(self, judgments: Sequence["ThreadJudgmentRow"]) -> None:
        with self.transaction():
            self._connection.executemany(
                "INSERT OR REPLACE INTO thread_judgments (handle_key, kind, name, who, confidence, judged_at, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(j.handle_key),
                        j.kind,
                        j.name,
                        j.who,
                        j.confidence,
                        j.judged_at.isoformat(),
                        j.model,
                    )
                    for j in judgments
                ],
            )

    def thread_judgments(self) -> dict[HandleKey, "ThreadJudgmentRow"]:
        rows = self._connection.execute(
            "SELECT handle_key, kind, name, who, confidence, judged_at, model FROM thread_judgments"
        ).fetchall()
        return {
            HandleKey(r[0]): ThreadJudgmentRow(
                handle_key=HandleKey(r[0]),
                kind=str(r[1]),
                name=str(r[2]),
                who=str(r[3]),
                confidence=float(r[4]),
                judged_at=datetime.fromisoformat(str(r[5])),
                model=str(r[6]),
            )
            for r in rows
        }

    def non_person_verdicts(self) -> dict[PersonId, str]:
        """Person ids omitted from contact lists, including explicit user exclusions."""
        rows = self._connection.execute(
            "SELECT person_id, verdict FROM person_judgments WHERE verdict IN ('GROUP', 'AUTOMATED', 'EXCLUDED')"
        ).fetchall()
        return {PersonId(row[0]): str(row[1]) for row in rows}

    # --- status ---

    def status(self) -> ArchiveStatus:
        per_account = self._connection.execute(
            "SELECT account, COUNT(*) FROM messages GROUP BY account"
        ).fetchall()
        bounds = self._connection.execute(
            "SELECT MIN(ts), MAX(ts) FROM messages"
        ).fetchone()
        contact_count = self._connection.execute(
            "SELECT COUNT(*) FROM contacts"
        ).fetchone()[0]
        event_count = self._connection.execute(
            "SELECT COUNT(*) FROM calendar_events"
        ).fetchone()[0]
        raw_count = self._connection.execute(
            "SELECT COUNT(*) FROM raw_records"
        ).fetchone()[0]
        text_count = self._connection.execute(
            "SELECT COUNT(*) FROM beeper_messages"
        ).fetchone()[0]
        chat_count = self._connection.execute(
            "SELECT COUNT(*) FROM beeper_chats"
        ).fetchone()[0]
        note_count = self._connection.execute("SELECT COUNT(*) FROM notes").fetchone()[
            0
        ]
        meeting_count = self._connection.execute(
            "SELECT COUNT(*) FROM meetings"
        ).fetchone()[0]
        return ArchiveStatus(
            text_message_count=int(text_count),
            chat_count=int(chat_count),
            note_count=int(note_count),
            recorded_meeting_count=int(meeting_count),
            message_count_by_account={str(row[0]): int(row[1]) for row in per_account},
            oldest_message_at=_from_epoch(bounds[0]) if bounds[0] is not None else None,
            newest_message_at=_from_epoch(bounds[1]) if bounds[1] is not None else None,
            contact_count=int(contact_count),
            calendar_event_count=int(event_count),
            raw_record_count=int(raw_count),
        )

    def execute_read(
        self, sql: str, parameters: Sequence[Any]
    ) -> list[tuple[Any, ...]]:
        """Run a read-only query; the CRM app's views build their queries on top of this."""
        return [
            tuple(row)
            for row in self._connection.execute(sql, tuple(parameters)).fetchall()
        ]


@pure
def _message_from_row(row: tuple[Any, ...]) -> ParsedEmail:
    from_addr = str(row[6])
    return ParsedEmail(
        account=AccountEmail(row[0]),
        message_id=GmailMessageId(row[1]),
        thread_id=GmailThreadId(row[2]),
        timestamp=_from_epoch(row[3]),
        direction=Direction(row[4]),
        sender=MessageParticipant(name=row[5], address=EmailAddress(from_addr))
        if from_addr
        else None,
        to_recipients=_participants_from_json(row[7]),
        cc_recipients=_participants_from_json(row[8]),
        subject=row[9],
        snippet=row[10],
        text=row[11],
        label_ids=tuple(json.loads(row[12])),
        is_automated=bool(row[13]),
        rfc_message_id=row[14],
        in_reply_to=row[15],
    )


@contextmanager
def open_archive_store(
    path: Path, lock_wait_seconds: int = DEFAULT_LOCK_WAIT_SECONDS
) -> Iterator[ArchiveStore]:
    store = ArchiveStore(path=path)
    store.open(lock_wait_seconds)
    try:
        yield store
    finally:
        store.close()


@contextmanager
def open_existing_archive_store(path: Path) -> Iterator[ArchiveStore]:
    """Open an archive whose schema has already been prepared, avoiding writer contention."""
    store = ArchiveStore(path=path)
    store.open_existing()
    try:
        yield store
    finally:
        store.close()
