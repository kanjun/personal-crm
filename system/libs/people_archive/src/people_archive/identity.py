import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

from people_archive.assertions import (
    Decision,
    assertions_path,
    auto_decisions_path,
    load_assertions,
)
from people_archive.cleanup_ledger import (
    load_overlay,
    original_ids_for_undo,
    save_aliases,
)
from people_archive.data_types import Direction
from people_archive.gmail_parse import _AUTOMATED_SENDER_PATTERN
from people_archive.hand_kept_contacts import (
    hand_kept_name_and_organization,
    load_hand_kept_contacts,
)
from people_archive.primitives import (
    AccountEmail,
    EmailAddress,
    HandleKey,
    HandleNetwork,
    PersonId,
    PhoneNumber,
    handle_key,
)
from people_archive.settings import load_settings
from people_archive.store import ArchiveStore, ResolvedPersonRow

# Tier thresholds on the relationship score below. Tier 1 is "we clearly know each other",
# tier 2 "we have talked", tier 3 "they exist in my mail".
_TIER_1_SCORE: Final[int] = 20
_TIER_2_SCORE: Final[int] = 4
_SENT_WEIGHT: Final[int] = 3
_RECEIVED_HUMAN_WEIGHT: Final[int] = 1
_MEETING_WEIGHT: Final[int] = 4
_CONTACT_CARD_WEIGHT: Final[int] = 3
# A day of texting either way is worth about an email exchange.
_TEXT_DAY_WEIGHT: Final[int] = 2
# the user's own companies: people there are colleagues, kept apart from the outside network.
# Categories in the hand-kept contacts file that mean "works with me".
# Domains that only ever carry relay aliases and notification senders (event platforms' anonymized addresses,
# marketing and support mailers, bounce handlers): an address there is not how a person is reached.
_RELAY_DOMAIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(^|\.)(luma-mail\.com|mktomail\.com|hubspotemail\.net|breezy-mail\.com|intercom-mail\.com|bounces\.google\.com|"
    r"substackinc\.com|agentmail\.cc|privaterelay\.appleid\.com|txt\.voice\.google\.com|mms\.att\.net|"
    r"sendoso\.com|mailchimpapp\.net|sendgrid\.net|amazonses\.com|rsgsv\.net|mcsv\.net)$"
    r"|^(leave|unsub)[.-]|^(em|e|news|marketing|go|links?|click|t|info|reply|bounce)\.",
    re.IGNORECASE,
)
# North American toll-free prefixes: marketing, verification codes and support lines, never a person.
_TOLL_FREE_PREFIXES: Final[tuple[str, ...]] = ("+1800", "+1833", "+1844", "+1855", "+1866", "+1877", "+1888")
# Thread verdicts that keep a number off the people list: bots, and strangers or one-off transactions.
_NOT_A_CONTACT_THREAD_KINDS: Final[frozenset[str]] = frozenset({"automated", "cold"})
# Google meeting rooms and resource calendars show up as attendees; they are not people.
_RESOURCE_CALENDAR_SUFFIX: Final[str] = "@resource.calendar.google.com"
_GROUP_CALENDAR_PREFIX: Final[str] = "c_"
# Local parts that mark an address as a role or system mailbox rather than a person.
_ROLE_MAILBOX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(admin|accounts?|ap|ar|billing|careers|contact|help|hr|jobs|legal|marketing|media|office|ops|"
    r"reply|replies|hey|hi|mail|email|digest|community|membership|crew|concierge|failed-payments|card-expiring|advisor|"
    r"payroll|press|privacy|recruiting|sales|security|service|shipping|tickets?|webmaster|events?|"
    r"all|everyone|team|staff|eng|engineering|research|product|capabilities|design|hiring|finance|people|it|"
    r"reminders?|calendar|notifications?|sf|nyc|london|remote|founders|board|investors|interns?|updates?|"
    r"announce(ments)?|general|random|social|lunch|room-[^@]*|[^@]*-team|[^@]*-group|[^@]*-noreply)(\+[^@]*)?@",
    re.IGNORECASE,
)
# Signature scan: the closing lines of a message someone wrote, looking for "First Last".
_SIGNATURE_TAIL_LINES: Final[int] = 12
_NAME_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^([A-Z][a-zA-Z'\-]+(?: [A-Z][a-zA-Z'\-]+){1,2})\s*$")


class HandleStats(FrozenModel):
    """Interaction counts for one handle, aggregated from the archive."""

    received_human_count: int = Field(description="Messages from this address that were not automated")
    received_automated_count: int = Field(description="Messages from this address that were automated")
    sent_count: int = Field(description="Messages the user sent with this address on To or Cc")
    cc_received_count: int = Field(description="Received messages with this address alongside the user")
    meeting_count: int = Field(description="Calendar events with this address as an attendee")
    text_days_sent: int = Field(description="Days on which the user texted this handle")
    text_days_received: int = Field(description="Days on which this handle texted the user")
    first_seen: datetime | None = Field(description="Earliest interaction")
    last_seen: datetime | None = Field(description="Latest interaction")


class ResolvedPerson(FrozenModel):
    """One person as the identity pass sees them: handles, best name, tier, and counts."""

    person_id: PersonId = Field(description="Stable id")
    canonical_name: str = Field(description="Best display name")
    aliases: tuple[str, ...] = Field(description="Other names seen for the same handles")
    emails: tuple[EmailAddress, ...] = Field(description="All email handles")
    phones: tuple[PhoneNumber, ...] = Field(description="All phone handles")
    chat_handles: tuple[HandleKey, ...] = Field(description="Chat-network handles with no phone or email (x:, imessage:, ...)")
    organization: str = Field(description="Company from a contact card, else the most common email domain's name")
    title: str = Field(description="Job title from a contact card; may be empty")
    tier: int = Field(description="1 (close) to 3 (peripheral)")
    is_human: bool = Field(description="False for robots, lists, and role mailboxes")
    is_colleague: bool = Field(description="Has an address at one of the user's own companies")
    hand_kept_category: str = Field(description="the user's category from the user's contacts file, if listed there")
    score: int = Field(description="Relationship score the tier is derived from")
    received_human_count: int = Field(description="Human messages from them")
    received_automated_count: int = Field(description="Automated messages from them")
    sent_count: int = Field(description="Messages the user sent to them")
    meeting_count: int = Field(description="Meetings shared")
    text_days_sent: int = Field(description="Days the user texted them")
    text_days_received: int = Field(description="Days they texted the user")
    first_contact: datetime | None = Field(description="Earliest interaction")
    last_contact: datetime | None = Field(description="Latest interaction")


class _UnionFind:
    """Plain union-find over handle keys; the only mutable helper in this module."""

    def __init__(self) -> None:
        self._parent_by_key: dict[HandleKey, HandleKey] = {}
        # Pairs of handle sets the user ruled are different people; a union that would join them is skipped.
        self._forbidden_by_key: dict[HandleKey, set[HandleKey]] = defaultdict(set)
        self._members_by_root: dict[HandleKey, set[HandleKey]] = {}

    def forbid(self, side_a: frozenset[HandleKey], side_b: frozenset[HandleKey]) -> None:
        for key in side_a:
            self._forbidden_by_key[key].update(side_b)
        for key in side_b:
            self._forbidden_by_key[key].update(side_a)

    def _would_cross_a_split(self, root_a: HandleKey, root_b: HandleKey) -> bool:
        """Check only constraints attached to the smaller cluster, not every split ever recorded."""
        if len(self._members_by_root[root_a]) > len(self._members_by_root[root_b]):
            root_a, root_b = root_b, root_a
        return any(other in self._parent_by_key and self.find(other) == root_b
                   for key in self._members_by_root[root_a]
                   for other in self._forbidden_by_key.get(key, ()))

    def add(self, key: HandleKey) -> None:
        if key not in self._parent_by_key:
            self._parent_by_key[key] = key
            self._members_by_root[key] = {key}

    def find(self, key: HandleKey) -> HandleKey:
        root = key
        while self._parent_by_key[root] != root:
            root = self._parent_by_key[root]
        current = key
        while self._parent_by_key[current] != root:
            parent = self._parent_by_key[current]
            self._parent_by_key[current] = root
            current = parent
        return root

    def union(self, key_a: HandleKey, key_b: HandleKey) -> None:
        self.add(key_a)
        self.add(key_b)
        root_a = self.find(key_a)
        root_b = self.find(key_b)
        if root_a != root_b and not (self._forbidden_by_key and self._would_cross_a_split(root_a, root_b)):
            kept, absorbed = min(root_a, root_b), max(root_a, root_b)
            self._parent_by_key[absorbed] = kept
            self._members_by_root[kept].update(self._members_by_root.pop(absorbed))

    def keys(self) -> list[HandleKey]:
        return list(self._parent_by_key)

    def keys_set(self) -> AbstractSet[HandleKey]:
        return self._parent_by_key.keys()

    def clusters(self) -> dict[HandleKey, list[HandleKey]]:
        grouped: dict[HandleKey, list[HandleKey]] = defaultdict(list)
        for key in self._parent_by_key:
            grouped[self.find(key)].append(key)
        return {root: sorted(members) for root, members in grouped.items()}


@pure
def _epoch_to_datetime(value: int | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value, tz=timezone.utc)


def own_addresses(store: ArchiveStore, accounts: Sequence[AccountEmail]) -> set[EmailAddress]:
    """Only explicitly configured self addresses and connected account addresses are me."""
    settings = load_settings()
    return {EmailAddress(str(a)) for a in [*accounts, *settings.accounts, *settings.self_addresses]}



@pure
def _local_part_first_token(address: str) -> str:
    """'user.name+tag@example.com' -> 'user': the piece of the local part before any dot, plus, or underscore."""
    return re.split(r"[.+_]", address.split("@", 1)[0].lower(), maxsplit=1)[0]


def _handle_stats_from_messages(store: ArchiveStore, own: set[EmailAddress]) -> dict[EmailAddress, dict[str, int]]:
    """Per-address counters from the participants table, in one aggregate query."""
    rows = store.execute_read(
        "SELECT p.address, p.role, m.direction, m.is_automated, COUNT(*), MIN(p.ts), MAX(p.ts) "
        "FROM message_participants p JOIN messages m ON m.account = p.account AND m.message_id = p.message_id "
        "GROUP BY p.address, p.role, m.direction, m.is_automated",
        (),
    )
    counters: dict[EmailAddress, dict[str, int]] = defaultdict(lambda: {
        "received_human": 0, "received_automated": 0, "sent": 0, "cc_received": 0, "first": 0, "last": 0,
    })
    for address_raw, role, direction, is_automated, count, first_ts, last_ts in rows:
        address = EmailAddress(str(address_raw))
        if address in own:
            continue
        entry = counters[address]
        if role == "from" and direction == Direction.RECEIVED.value:
            entry["received_automated" if is_automated else "received_human"] += int(count)
        elif direction == Direction.SENT.value and role in ("to", "cc"):
            entry["sent"] += int(count)
        elif direction == Direction.RECEIVED.value and role in ("to", "cc"):
            entry["cc_received"] += int(count)
        else:
            pass
        entry["first"] = int(first_ts) if entry["first"] == 0 else min(entry["first"], int(first_ts))
        entry["last"] = max(entry["last"], int(last_ts))
    return counters


def _names_by_address(store: ArchiveStore) -> dict[EmailAddress, Counter[str]]:
    rows = store.execute_read(
        "SELECT address, name, COUNT(*) FROM message_participants WHERE name != '' GROUP BY address, name", ()
    )
    names: dict[EmailAddress, Counter[str]] = defaultdict(Counter)
    for address_raw, name, count in rows:
        names[EmailAddress(str(address_raw))][str(name)] += int(count)
    return names


def _meeting_counts(store: ArchiveStore, own: set[EmailAddress]) -> tuple[dict[EmailAddress, int], dict[EmailAddress, tuple[int, int]], dict[EmailAddress, Counter[str]]]:
    """Meeting series per attendee address (a weekly standup counts once), their first/last times, and invite names."""
    rows = store.execute_read(
        "SELECT start_ts, series_id, attendees_json FROM calendar_events WHERE start_ts <= ?",
        (int(datetime.now(timezone.utc).timestamp()),),
    )
    series_by_address: dict[EmailAddress, set[str]] = defaultdict(set)
    bounds: dict[EmailAddress, tuple[int, int]] = {}
    names: dict[EmailAddress, Counter[str]] = defaultdict(Counter)
    for start_ts, series_id, attendees_json in rows:
        for address_raw, name, _status, _is_self in json.loads(attendees_json):
            address = EmailAddress(str(address_raw))
            if address in own or address.endswith(_RESOURCE_CALENDAR_SUFFIX) or address.startswith(_GROUP_CALENDAR_PREFIX):
                continue
            series_by_address[address].add(str(series_id))
            first, last = bounds.get(address, (int(start_ts), int(start_ts)))
            bounds[address] = (min(first, int(start_ts)), max(last, int(start_ts)))
            if name:
                names[address][str(name)] += 1
    counts = {address: len(series) for address, series in series_by_address.items()}
    return counts, bounds, names


class TextStats(FrozenModel):
    """What the texts say about one chat handle."""

    days_sent: int = Field(description="Days on which the user wrote to this handle in a one-to-one chat")
    days_received: int = Field(description="Days on which this handle wrote, in any chat")
    first_ts: int = Field(description="Earliest text")
    last_ts: int = Field(description="Latest text")
    has_direct_chat: bool = Field(description="A one-to-one chat with this handle exists")


def _text_stats(store: ArchiveStore) -> tuple[dict[HandleKey, TextStats], dict[HandleKey, Counter[str]]]:
    """Per participant handle: the days texted, and the names seen on chats.

    A day counts as received only when this handle itself wrote; being a member of a busy group
    is not contact. the user's own messages count as sent only in one-to-one chats, where the one
    other member is the person the user wrote to.
    """
    rows = store.execute_read(
        # The sender join is LEFT: on some networks a message's sender id is not among the chat's
        # participant ids; in a one-to-one chat the one other member is then the sender.
        "SELECT p.handle_key, m.is_sender, m.ts, c.chat_type FROM beeper_messages m "
        "JOIN beeper_chats c ON c.chat_id = m.chat_id "
        "JOIN beeper_participants p ON p.chat_id = m.chat_id AND p.is_self = 0 "
        "LEFT JOIN beeper_participants sender ON sender.chat_id = m.chat_id AND sender.participant_id = m.sender_id "
        "WHERE (m.is_sender = 1 AND c.chat_type = 'single') OR (m.is_sender = 0 AND sender.handle_key = p.handle_key) "
        "OR (m.is_sender = 0 AND sender.participant_id IS NULL AND c.chat_type = 'single') "
        "GROUP BY p.handle_key, m.is_sender, m.ts / 86400, c.chat_type",
        (),
    )
    days_sent: dict[HandleKey, set[int]] = defaultdict(set)
    days_received: dict[HandleKey, set[int]] = defaultdict(set)
    bounds: dict[HandleKey, tuple[int, int]] = {}
    direct: set[HandleKey] = set()
    for key_raw, is_sender, ts, chat_type in rows:
        key = HandleKey(str(key_raw))
        day = int(ts) // 86400
        (days_sent if is_sender else days_received)[key].add(day)
        if str(chat_type) == "single":
            direct.add(key)
        first, last = bounds.get(key, (int(ts), int(ts)))
        bounds[key] = (min(first, int(ts)), max(last, int(ts)))
    stats = {
        key: TextStats(
            days_sent=len(days_sent.get(key, ())), days_received=len(days_received.get(key, ())),
            first_ts=bounds[key][0], last_ts=bounds[key][1], has_direct_chat=key in direct,
        )
        for key in bounds
    }
    names: dict[HandleKey, Counter[str]] = defaultdict(Counter)
    for key_raw, full_name, count in store.execute_read(
        "SELECT handle_key, full_name, COUNT(*) FROM beeper_participants WHERE is_self = 0 AND full_name != '' AND full_name != phone GROUP BY handle_key, full_name", ()
    ):
        names[HandleKey(str(key_raw))][str(full_name)] += int(count)
    return stats, names


@pure
def _is_relay_address(address: EmailAddress) -> bool:
    return bool(_RELAY_DOMAIN_PATTERN.search(address.split("@", 1)[1]))


@pure
def _is_role_or_robot_address(address: EmailAddress) -> bool:
    return bool(_AUTOMATED_SENDER_PATTERN.match(address)) or bool(_ROLE_MAILBOX_PATTERN.match(address)) or _is_relay_address(address)


@pure
def _organization_from_domain(address: EmailAddress) -> str:
    domain = address.split("@", 1)[1]
    free_mail = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me", "aol.com", "live.com", "msn.com", "hey.com", "fastmail.com"}
    if domain in free_mail:
        return ""
    return domain.rsplit(".", 1)[0].split(".")[-1].capitalize()


@pure
def _pick_canonical_name(
    contact_names: Sequence[str], header_names: Counter[str], invite_names: Counter[str], emails: Sequence[EmailAddress],
) -> tuple[str, tuple[str, ...]]:
    """Contact card name first, then the most common name seen in headers, invites, and chats; local part (or the handle) as a last resort."""
    combined: Counter[str] = Counter()
    combined.update(header_names)
    combined.update(invite_names)
    ranked = [name for name, _ in combined.most_common() if name and "@" not in name]
    candidates = [name for name in contact_names if name] + ranked
    if not candidates:
        first_handle = str(emails[0]) if emails else ""
        local_part = first_handle.split("@", 1)[0] if "@" in first_handle else first_handle.split(":", 1)[-1]
        return local_part.replace(".", " ").title(), ()
    canonical = candidates[0]
    aliases = tuple(dict.fromkeys(name for name in candidates[1:] if name.lower() != canonical.lower()))
    return canonical, aliases


@pure
def _score_and_tier(sent: int, received_human: int, meetings: int, has_contact_card: bool, text_days: int) -> tuple[int, int]:
    score = (sent * _SENT_WEIGHT + received_human * _RECEIVED_HUMAN_WEIGHT + meetings * _MEETING_WEIGHT
             + (_CONTACT_CARD_WEIGHT if has_contact_card else 0) + text_days * _TEXT_DAY_WEIGHT)
    if score >= _TIER_1_SCORE:
        return score, 1
    elif score >= _TIER_2_SCORE:
        return score, 2
    else:
        return score, 3


def _signature_names(store: ArchiveStore, first_name_by_address: Mapping[EmailAddress, str]) -> dict[EmailAddress, Counter[str]]:
    """For addresses known only by a first name, full names found at the foot of messages they sent."""
    found: dict[EmailAddress, Counter[str]] = defaultdict(Counter)
    if not first_name_by_address:
        return found
    addresses = list(first_name_by_address)
    for start in range(0, len(addresses), 500):
        chunk = addresses[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        last_rowid = 0
        has_more = True
        while has_more:
            # Keep whole originals for correct signature boundaries, but never materialize
            # every message body for hundreds of addresses in one Python allocation.
            rows = store.execute_read(
                f"SELECT rowid, from_addr, text FROM messages WHERE is_automated = 0 AND from_addr IN ({placeholders}) AND rowid > ? ORDER BY rowid LIMIT 32",
                [*chunk, last_rowid],
            )
            has_more = bool(rows)
            for rowid, address_raw, text in rows:
                last_rowid = int(rowid)
                address = EmailAddress(str(address_raw))
                first = first_name_by_address[address].lower()
                tail = [line.strip() for line in str(text).strip().splitlines()[-_SIGNATURE_TAIL_LINES:]]
                for line in tail:
                    match = _NAME_LINE_PATTERN.match(line)
                    if match and match.group(1).split()[0].lower() == first:
                        found[address][match.group(1)] += 1
    return found


def resolve_people(store: ArchiveStore, accounts: Sequence[AccountEmail]) -> list[ResolvedPerson]:
    """Cluster every handle in the archive into people on hard evidence and score each cluster.

    Hard evidence today: a contact card listing several emails and phones. Person ids are
    kept stable across runs through the store's handle-to-person map.
    """
    settings = load_settings()
    own = own_addresses(store, accounts)
    message_counters = _handle_stats_from_messages(store, own)
    header_names = _names_by_address(store)
    meeting_counts, meeting_bounds, invite_names = _meeting_counts(store, own)
    text_stats, text_names = _text_stats(store)
    thread_judgments = store.thread_judgments()
    # Where the header only ever gave a first name, look for the full name in signatures.
    single_word_first_names = {
        address: names.most_common(1)[0][0].strip()
        for address, names in header_names.items()
        if " " not in names.most_common(1)[0][0].strip() and "@" not in names.most_common(1)[0][0]
    }
    for address, signature_names in _signature_names(store, single_word_first_names).items():
        # A signature name outranks the bare first name from the header for that address.
        header_names[address] = signature_names + header_names[address]

    # Every address seen anywhere is a handle; contact cards add phones and the merges.
    union = _UnionFind()
    assertions = load_assertions(assertions_path())
    # The merge judge's confident calls: below the user's own decisions, above every rule.
    auto_decisions = load_assertions(auto_decisions_path())
    archive_path = Path(next(str(row[2]) for row in store.execute_read("PRAGMA database_list", ()) if row[1] == "main"))
    cleanup = load_overlay(archive_path.parent / "cleanup.sqlite") if settings.cleanup_enabled else []
    current_ids = store.person_id_by_handle()
    protected_ids = {str(row[0]) for row in store.execute_read("SELECT person_id FROM person_edits UNION SELECT person_id FROM list_decisions", ())}
    cleanup = [item for item in cleanup if item["kind"] == "DIFFERENT" or not any(str(current_ids.get(HandleKey(h), "")) in protected_ids for h in item["a"] + item["b"])]
    # Machine splits guard automatic rules too; explicit user SAME overrides machine splits.
    user_same = [set(a.handles_a) | set(a.handles_b) for a in assertions if a.decision == Decision.SAME]
    for item in cleanup:
        if item["kind"] == "DIFFERENT" and not any(set(item["a"]) & group and set(item["b"]) & group for group in user_same):
            union.forbid(frozenset(HandleKey(h) for h in item["a"]), frozenset(HandleKey(h) for h in item["b"]))
    for assertion in auto_decisions:
        if assertion.decision == Decision.DIFFERENT and not any(set(assertion.handles_a) & group and set(assertion.handles_b) & group for group in user_same):
            union.forbid(frozenset(assertion.handles_a), frozenset(assertion.handles_b))
    for assertion in assertions:
        if assertion.decision == Decision.DIFFERENT:
            union.forbid(frozenset(assertion.handles_a), frozenset(assertion.handles_b))
    for address in list(message_counters) + list(meeting_counts):
        union.add(handle_key(HandleNetwork.EMAIL, address))
    for key in text_stats:
        union.add(key)
    contact_names_by_handle: dict[HandleKey, list[str]] = defaultdict(list)
    contact_org_by_handle: dict[HandleKey, tuple[str, str]] = {}
    handle_has_card: set[HandleKey] = set()
    for card in store.iter_contacts():
        keys = [handle_key(HandleNetwork.EMAIL, e) for e in card.emails if e not in own]
        keys += [handle_key(HandleNetwork.PHONE, p) for p in card.phones]
        for key in keys:
            union.add(key)
            handle_has_card.add(key)
            if card.display_name:
                contact_names_by_handle[key].append(card.display_name)
            if card.organization or card.title:
                contact_org_by_handle[key] = (card.organization, card.title)
        for key in keys[1:]:
            union.union(keys[0], key)

    # the user's hand-kept contacts file: the user's own words, so its name wins and its emails merge.
    hand_kept_category_by_handle: dict[HandleKey, str] = {}
    for contact in load_hand_kept_contacts(Path(settings.hand_kept_contacts_path) if settings.hand_kept_contacts_path else None):
        name, organization = hand_kept_name_and_organization(contact)
        keys = [handle_key(HandleNetwork.EMAIL, e) for e in contact.emails if e not in own]
        for key in keys:
            union.add(key)
            handle_has_card.add(key)
            hand_kept_category_by_handle[key] = contact.category
            if name:
                contact_names_by_handle[key].insert(0, name)
            if organization and key not in contact_org_by_handle:
                contact_org_by_handle[key] = (organization, "")
        for key in keys[1:]:
            union.union(keys[0], key)

    # Same full name on two addresses with the same local part (josh@a.com / josh@b.com) is
    # taken as hard evidence; the same full name alone is only proposed for review.
    every_email = {EmailAddress(key.split(":", 1)[1]) for key in union.keys() if key.startswith("email:")}
    _merge_by_name_and_local_part(union, every_email, header_names, invite_names, contact_names_by_handle)
    _merge_phones_to_emails_by_unique_full_name(union, header_names, invite_names, text_names)
    # The judge's "same person" calls join people the rules only proposed; a split the user recorded still blocks them.
    # the user's own "same person" decisions come last and outrank every rule above.
    for assertion in [*(a for a in auto_decisions if a.decision == Decision.SAME), *assertions]:
        if assertion.decision == Decision.SAME:
            handles = [*assertion.handles_a, *assertion.handles_b]
            for key in handles:
                union.add(key)
            for key in handles[1:]:
                union.union(handles[0], key)

    for item in cleanup:
        if item["kind"] == "SAME":
            keys = [HandleKey(h) for h in item["a"] + item["b"]]
            for key in keys:
                union.add(key)
            for key in keys[1:]:
                union.union(keys[0], key)

    names_by_handle: dict[str, str] = {}
    for item in cleanup:
        if item["kind"] == "NAME":
            for handle in item["a"]:
                names_by_handle[handle] = item["value"]
    clusters = union.clusters()
    existing_ids = store.person_id_by_handle()
    existing_ids.update({HandleKey(h):PersonId(p) for h,p in (original_ids_for_undo(archive_path.parent / "cleanup.sqlite") if settings.cleanup_enabled else {}).items()})
    person_id_by_root = _assign_stable_person_ids(clusters, existing_ids)
    ruled_not_people = store.non_person_verdicts()
    people: list[ResolvedPerson] = []
    for root, members in clusters.items():
        # A relay alias that rode in with a real address goes last, so it is never the address shown for the person.
        emails = tuple(sorted((EmailAddress(key.split(":", 1)[1]) for key in members if key.startswith("email:")), key=_is_relay_address))
        phones = tuple(PhoneNumber(key.split(":", 1)[1]) for key in members if key.startswith("phone:"))
        chat_handles = tuple(key for key in members if not key.startswith("email:") and not key.startswith("phone:"))
        if not emails and not phones and not chat_handles:
            continue
        person_id = person_id_by_root[root]
        stats = _merge_stats(emails, members, message_counters, meeting_counts, meeting_bounds, text_stats)
        contact_names = [name for key in members for name in contact_names_by_handle.get(key, [])]
        names_in_headers: Counter[str] = Counter()
        names_on_invites: Counter[str] = Counter()
        for email in emails:
            names_in_headers.update(header_names.get(email, Counter()))
            names_on_invites.update(invite_names.get(email, Counter()))
        for key in members:
            names_on_invites.update(text_names.get(key, Counter()))
        canonical, aliases = _pick_canonical_name(contact_names, names_in_headers, names_on_invites, emails or tuple(phones) or chat_handles)
        is_named = bool(contact_names) or bool(names_in_headers) or bool(names_on_invites)
        thread_verdicts = [thread_judgments[key] for key in members if key in thread_judgments]
        # A name read out of the text thread itself is the last resort, below every header, invite and card.
        thread_name = next((verdict.name for verdict in thread_verdicts if verdict.name), "")
        if not is_named and thread_name:
            canonical = thread_name
        supported_names = [names_by_handle[key] for key in members if key in names_by_handle]
        if supported_names:
            replacement = supported_names[-1]
            aliases = tuple(dict.fromkeys([canonical, *aliases])) if replacement != canonical else aliases
            canonical = replacement
        organization, title = next((contact_org_by_handle[key] for key in members if key in contact_org_by_handle), ("", ""))
        has_card = any(key in handle_has_card for key in members)
        score, tier = _score_and_tier(stats.sent_count, stats.received_human_count, stats.meeting_count, has_card, stats.text_days_sent + stats.text_days_received)
        # A bare number or handle nobody has put a name to, seen only in group chats, is not someone to list;
        # nor is a toll-free line, nor a thread the judge read as a bot or a stranger.
        has_direct_chat = any(key in text_stats and text_stats[key].has_direct_chat for key in members)
        is_toll_free_only = not emails and bool(phones) and all(_is_toll_free(phone) for phone in phones)
        is_not_a_contact = not emails and not has_card and any(verdict.kind in _NOT_A_CONTACT_THREAD_KINDS for verdict in thread_verdicts)
        is_human = (
            _is_human(emails, stats, has_card) and person_id not in ruled_not_people
            and (bool(emails) or has_card or is_named or has_direct_chat)
            and not is_toll_free_only and not is_not_a_contact
        )
        hand_kept_category = next((hand_kept_category_by_handle[key] for key in members if key in hand_kept_category_by_handle), "")
        # A colleague is anyone at one of the user's companies -- by address, or by the user's own contacts file.
        is_colleague = (
            any(email.split("@", 1)[1] in settings.colleague_domains for email in emails)
            or hand_kept_category in settings.colleague_categories
        )
        people.append(ResolvedPerson(
            person_id=person_id, canonical_name=canonical, aliases=aliases, emails=emails, phones=phones, chat_handles=chat_handles,
            organization=organization or (_organization_from_domain(emails[0]) if emails else ""), title=title,
            tier=tier, is_human=is_human, is_colleague=is_colleague, hand_kept_category=hand_kept_category, score=score,
            received_human_count=stats.received_human_count, received_automated_count=stats.received_automated_count,
            sent_count=stats.sent_count, meeting_count=stats.meeting_count,
            text_days_sent=stats.text_days_sent, text_days_received=stats.text_days_received,
            first_contact=stats.first_seen, last_contact=stats.last_seen,
        ))
    logger.info("Resolved {} people ({} human)", len(people), sum(1 for p in people if p.is_human))
    return people


@pure
def _normalized_full_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z ]", "", name.lower()).strip()
    return cleaned if len(cleaned.split()) >= 2 else ""


def _merge_by_name_and_local_part(
    union: _UnionFind,
    all_addresses: AbstractSet[EmailAddress],
    header_names: Mapping[EmailAddress, Counter[str]],
    invite_names: Mapping[EmailAddress, Counter[str]],
    contact_names_by_handle: Mapping[HandleKey, Sequence[str]],
) -> None:
    """Union email handles that share a two-word display name and the same local part."""
    by_name_and_local: dict[tuple[str, str], list[HandleKey]] = defaultdict(list)
    for address in all_addresses:
        key = handle_key(HandleNetwork.EMAIL, address)
        names: Counter[str] = Counter()
        names.update(header_names.get(address, Counter()))
        names.update(invite_names.get(address, Counter()))
        names.update({name: 1 for name in contact_names_by_handle.get(key, [])})
        # Every full name ever seen for the address counts, not only the commonest: headers
        # often carry a bare first name hundreds of times and the full name a handful.
        for full_name in {_normalized_full_name(name) for name in names} - {""}:
            by_name_and_local[(full_name, address.split("@", 1)[0])].append(key)
    for keys in by_name_and_local.values():
        for key in keys[1:]:
            union.union(keys[0], key)
    # Inside the user's own companies a local part is one person across the company's domains.
    colleague_domains = frozenset(load_settings().colleague_domains)
    internal_by_local: dict[str, list[HandleKey]] = defaultdict(list)
    for address in all_addresses:
        local_part, domain = address.split("@", 1)
        if domain in colleague_domains and not _is_role_or_robot_address(address):
            internal_by_local[local_part].append(handle_key(HandleNetwork.EMAIL, address))
    for keys in internal_by_local.values():
        for key in keys[1:]:
            union.union(keys[0], key)
    # A bare first name plus a matching local part is NOT enough on its own: "ben@" with the
    # header "Ben" exists at dozens of companies. Outside the user's own domains, only a full
    # name joins addresses.


def _merge_phones_to_emails_by_unique_full_name(
    union: _UnionFind,
    header_names: Mapping[EmailAddress, Counter[str]],
    invite_names: Mapping[EmailAddress, Counter[str]],
    text_names: Mapping[HandleKey, Counter[str]],
) -> None:
    """A texter and an emailer with the same full name are one person -- but only when that
    full name belongs to exactly one email handle and exactly one phone handle, so common
    names never collapse strangers together."""
    emails_by_name: dict[str, set[HandleKey]] = defaultdict(set)
    for address in set(header_names) | set(invite_names):
        names: Counter[str] = Counter()
        names.update(header_names.get(address, Counter()))
        names.update(invite_names.get(address, Counter()))
        for full_name in {_normalized_full_name(name) for name in names} - {""}:
            emails_by_name[full_name].add(handle_key(HandleNetwork.EMAIL, address))
    phones_by_name: dict[str, set[HandleKey]] = defaultdict(set)
    for key, names in text_names.items():
        if not key.startswith("phone:"):
            continue
        for full_name in {_normalized_full_name(name) for name in names} - {""}:
            phones_by_name[full_name].add(key)
    for full_name, phone_keys in phones_by_name.items():
        email_keys = emails_by_name.get(full_name, set())
        email_roots = {union.find(key) for key in email_keys if key in union.keys_set()}
        if len(phone_keys) == 1 and len(email_roots) == 1:
            union.union(next(iter(phone_keys)), next(iter(email_keys)))


def find_merge_candidates(people: Sequence[ResolvedPerson]) -> list[tuple[PersonId, PersonId, str]]:
    """Pairs of distinct people sharing a two-word canonical name: proposed, never merged by this rule.
    Pairs the user or the merge judge already ruled different are left out."""
    ruled_different = [
        (frozenset(a.handles_a), frozenset(a.handles_b))
        for a in [*load_assertions(assertions_path()), *load_assertions(auto_decisions_path())]
        if a.decision == Decision.DIFFERENT
    ]
    by_name: dict[str, list[ResolvedPerson]] = defaultdict(list)
    for person in people:
        if not person.is_human:
            continue
        normalized = _normalized_full_name(person.canonical_name)
        if normalized:
            by_name[normalized].append(person)
    candidates: list[tuple[PersonId, PersonId, str]] = []
    for group in by_name.values():
        ordered = sorted(group, key=lambda p: str(p.person_id))
        for idx in range(1, len(ordered)):
            first_handles = set(_handle_keys_of(ordered[0]))
            other_handles = set(_handle_keys_of(ordered[idx]))
            if any((first_handles & a and other_handles & b) or (first_handles & b and other_handles & a) for a, b in ruled_different):
                continue
            candidates.append((ordered[0].person_id, ordered[idx].person_id, "same full name"))
    return candidates


@pure
def _handle_keys_of(person: ResolvedPerson) -> tuple[HandleKey, ...]:
    return (
        tuple(handle_key(HandleNetwork.EMAIL, e) for e in person.emails)
        + tuple(handle_key(HandleNetwork.PHONE, p) for p in person.phones)
        + tuple(person.chat_handles)
    )


@pure
def _is_toll_free(phone: PhoneNumber) -> bool:
    """Toll-free in either form the archive holds: +18445551234 or a bare 8445551234."""
    digits = phone.lstrip("+")
    national = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
    return len(national) == 10 and any(national.startswith(prefix[2:]) for prefix in _TOLL_FREE_PREFIXES)


@pure
def _is_human(emails: Sequence[EmailAddress], stats: HandleStats, has_contact_card: bool) -> bool:
    """A person, not a robot or a group mailbox: the user wrote to them, met them, texted with them, keeps a card for them, or they wrote the user's non-bulk mail."""
    if emails and all(_is_role_or_robot_address(email) for email in emails):
        return False
    if has_contact_card or stats.sent_count > 0 or stats.meeting_count > 0 or stats.text_days_sent > 0 or stats.text_days_received > 0:
        return True
    return stats.received_human_count > 0


@pure
def _merge_stats(
    emails: Sequence[EmailAddress],
    members: Sequence[HandleKey],
    message_counters: Mapping[EmailAddress, Mapping[str, int]],
    meeting_counts: Mapping[EmailAddress, int],
    meeting_bounds: Mapping[EmailAddress, tuple[int, int]],
    text_stats: Mapping[HandleKey, TextStats],
) -> HandleStats:
    received_human = received_automated = sent = cc_received = meetings = 0
    text_sent = text_received = 0
    first: int | None = None
    last: int | None = None
    for key in members:
        if key in text_stats:
            texts = text_stats[key]
            text_sent += texts.days_sent
            text_received += texts.days_received
            first = texts.first_ts if first is None else min(first, texts.first_ts)
            last = texts.last_ts if last is None else max(last, texts.last_ts)
    for email in emails:
        counters = message_counters.get(email)
        if counters is not None:
            received_human += counters["received_human"]
            received_automated += counters["received_automated"]
            sent += counters["sent"]
            cc_received += counters["cc_received"]
            if counters["first"]:
                first = counters["first"] if first is None else min(first, counters["first"])
                last = counters["last"] if last is None else max(last, counters["last"])
        meetings += meeting_counts.get(email, 0)
        if email in meeting_bounds:
            meeting_first, meeting_last = meeting_bounds[email]
            first = meeting_first if first is None else min(first, meeting_first)
            last = meeting_last if last is None else max(last, meeting_last)
    return HandleStats(
        received_human_count=received_human, received_automated_count=received_automated, sent_count=sent,
        cc_received_count=cc_received, meeting_count=meetings, text_days_sent=text_sent, text_days_received=text_received,
        first_seen=_epoch_to_datetime(first), last_seen=_epoch_to_datetime(last),
    )


@pure
def _assign_stable_person_ids(
    clusters: Mapping[HandleKey, Sequence[HandleKey]],
    existing_ids: Mapping[HandleKey, PersonId],
) -> dict[HandleKey, PersonId]:
    """Each cluster keeps the id most of its handles carried before; when an old person split into
    several clusters, the one with the most of its handles keeps the id and the rest get fresh ids."""
    claims: list[tuple[int, HandleKey, PersonId]] = []
    for root, members in clusters.items():
        votes: Counter[PersonId] = Counter(existing_ids[key] for key in members if key in existing_ids)
        for person_id, count in votes.items():
            claims.append((count, root, person_id))
    assigned: dict[HandleKey, PersonId] = {}
    taken: set[PersonId] = set()
    for count, root, person_id in sorted(claims, key=lambda claim: (-claim[0], str(claim[1]))):
        if root in assigned or person_id in taken:
            continue
        assigned[root] = person_id
        taken.add(person_id)
    for root in clusters:
        if root not in assigned:
            assigned[root] = PersonId.generate()
    return assigned


@pure
def _to_store_row(person: ResolvedPerson) -> ResolvedPersonRow:
    keys = (
        tuple(handle_key(HandleNetwork.EMAIL, e) for e in person.emails)
        + tuple(handle_key(HandleNetwork.PHONE, p) for p in person.phones)
        + tuple(person.chat_handles)
    )
    return ResolvedPersonRow(
        person_id=person.person_id, canonical_name=person.canonical_name, aliases=person.aliases,
        emails=tuple(str(e) for e in person.emails), phones=tuple(str(p) for p in person.phones), handle_keys=keys,
        organization=person.organization, title=person.title, tier=person.tier, is_human=person.is_human,
        is_colleague=person.is_colleague, hand_kept_category=person.hand_kept_category, score=person.score, received_human_count=person.received_human_count,
        received_automated_count=person.received_automated_count, sent_count=person.sent_count,
        meeting_count=person.meeting_count, text_days_sent=person.text_days_sent, text_days_received=person.text_days_received,
        first_contact=person.first_contact, last_contact=person.last_contact,
    )


def resolve_and_store_people(store: ArchiveStore, accounts: Sequence[AccountEmail]) -> list[ResolvedPerson]:
    existing = store.person_id_by_handle()
    people = resolve_people(store, accounts)
    store.replace_people([_to_store_row(p) for p in people], find_merge_candidates(people))
    archive_path = Path(next(str(row[2]) for row in store.execute_read("PRAGMA database_list", ()) if row[1] == "main"))
    if load_settings().cleanup_enabled:
        save_aliases(dict(existing), {str(h): str(p.person_id) for p in people for h in _handle_keys_of(p)}, archive_path.parent / "cleanup.sqlite")
    return people
