import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from markupsafe import escape
from people_archive.assertions import (
    assertions_path,
    auto_decisions_path,
    is_decided,
    load_assertions,
)
from people_archive.merge_judge import decision_for
from people_archive.primitives import AccountEmail, PersonId, SourceKind
from people_archive.store import (
    DOSSIER_FIELDS,
    ArchiveStore,
    MergeJudgmentRow,
    SocialProfileRow,
    parse_tag_list,
)
from pydantic import Field

# The list view pages in chunks; the person page shows the newest interactions first.
PEOPLE_PAGE_SIZE: Final[int] = 100
TIMELINE_LIMIT: Final[int] = 400
SEARCH_LIMIT: Final[int] = 60


class YearRow(FrozenModel):
    """One row of the activity heatmap: a year and its twelve monthly counts."""

    year: int = Field(description="Calendar year")
    months: tuple[int, ...] = Field(description="Interactions per month, January first")


class PersonSummary(FrozenModel):
    """One row of the people list."""

    person_id: str = Field(description="Stable id")
    name: str = Field(description="Canonical name")
    primary_email: str = Field(description="First email handle")
    organization: str = Field(description="Company")
    title: str = Field(description="Job title")
    tier: int = Field(description="1..3")
    is_colleague: bool = Field(description="Works at one of the user's companies")
    score: int = Field(description="Relationship score")
    sent_count: int = Field(description="Messages the user sent them")
    received_count: int = Field(description="Human messages from them")
    meeting_count: int = Field(description="Meeting series shared")
    last_contact: datetime | None = Field(description="Latest interaction")
    first_contact: datetime | None = Field(description="Earliest interaction")
    hand_kept_category: str = Field(description="Category from the contacts file, if any")
    phones: tuple[str, ...] = Field(description="Phone handles")
    text_days: int = Field(description="Days with texts either way")
    trace: tuple[int, ...] = Field(description="Interactions per month over the last 24 months, oldest first")
    heatmap: tuple[YearRow, ...] = Field(description="Interactions per month for the last HEATMAP_YEARS years, oldest year first")
    heatmap_peak: int = Field(description="Largest monthly count in the heatmap, for the color scale")
    roles: tuple[str, ...] = Field(default=(), description="Role tags: the user's edit, else the dossier's, else the cheap tagger's")
    interests: tuple[str, ...] = Field(default=(), description="Interest tags, same precedence")
    thread_note: str = Field(default="", description="Who the text thread says this is, for a number that was never saved as a contact")


# Ways to group the people list; the value is the label the page shows.
GROUP_BY_OPTIONS: Final[dict[str, str]] = {"role": "Role", "interest": "Interest", "organization": "Organization", "category": "Your category"}
# A grouped view looks at the closest people only; the full list stays paged.
GROUPED_PEOPLE_LIMIT: Final[int] = 800
GROUP_PREVIEW_SIZE: Final[int] = 12


class HeaderCounts(FrozenModel):
    """The archive counts the people page's header shows."""

    message_count_by_account: dict[str, int] = Field(description="Archived emails per inbox")
    calendar_event_count: int = Field(description="Archived calendar events")


def header_counts(store: ArchiveStore) -> HeaderCounts:
    """Only what the header shows, each answered from an index: the full archive status walks every raw record."""
    per_account = store.execute_read("SELECT account, COUNT(*) FROM messages GROUP BY account", [])
    events = store.execute_read("SELECT COUNT(*) FROM calendar_events", [])[0][0]
    return HeaderCounts(message_count_by_account={str(a): int(n) for a, n in per_account}, calendar_event_count=int(events))


class PeopleListFilters(FrozenModel):
    """What the people list is filtered and sorted by."""

    query: str = Field(description="Name, email, or organization substring")
    tier: int | None = Field(description="Only this tier, or all")
    group: str = Field(description="'' for everyone, 'colleagues' for colleagues only, 'network' for everyone else")
    sort: str = Field(description="'score' or 'recent'")
    page: int = Field(description="Zero-based page")
    group_by: str = Field(default="", description="'' for a flat list, else a key of GROUP_BY_OPTIONS")
    group_value: str = Field(default="", description="With group_by: show only this one group, in full")


class PeopleGroup(FrozenModel):
    """One heading of a grouped people list."""

    key: str = Field(description="The tag, organization, or category, as stored")
    label: str = Field(description="What the heading says")
    count: int = Field(description="People in the group")
    people: tuple[PersonSummary, ...] = Field(description="The preview (or all of them, when this group was picked)")


class PeopleListPage(FrozenModel):
    """A page of the people list plus the counts behind the filter chips."""

    people: tuple[PersonSummary, ...] = Field(description="This page")
    total_count: int = Field(description="Matches across all pages")
    human_count: int = Field(description="Humans in the archive (excluding colleagues)")
    colleague_count: int = Field(description="Colleagues in the archive")
    tier_counts: dict[int, int] = Field(description="Humans per tier (excluding colleagues)")
    groups: tuple[PeopleGroup, ...] = Field(default=(), description="When grouped: the headings, biggest first")
    grouped_count: int = Field(default=0, description="When grouped: how many of the closest people were looked at")


class TimelineItem(FrozenModel):
    """One interaction on a person's page."""

    kind: str = Field(description="'email' or 'meeting'")
    timestamp: datetime = Field(description="When")
    direction: str = Field(description="SENT / RECEIVED / OBSERVED")
    title: str = Field(description="Subject or event title")
    snippet: str = Field(description="Preview text")
    account: str = Field(description="Which of the user's accounts")
    source_id: str = Field(description="Message id or event id")
    thread_id: str = Field(description="Gmail thread id, when an email")
    is_automated: bool = Field(description="Bulk mail")


class MeetingSeries(FrozenModel):
    """Meetings with the same title, collapsed into one line."""

    title: str = Field(description="Event title")
    count: int = Field(description="How many occurrences")
    first: datetime = Field(description="Earliest occurrence")
    last: datetime = Field(description="Latest occurrence")
    account: str = Field(description="Calendar account")
    latest_event_id: str = Field(description="Id of the latest occurrence, for the original-record link")


class YearActivity(FrozenModel):
    """Interaction counts for one calendar year, for the activity strip."""

    year: int = Field(description="Calendar year")
    sent: int = Field(description="Emails the user sent them")
    received: int = Field(description="Human emails from them")
    meetings: int = Field(description="Meetings that year")


class ChatSummary(FrozenModel):
    """One chat the person is in, for the person page."""

    chat_id: str = Field(description="Beeper chat id")
    network: str = Field(description="Network")
    title: str = Field(description="Chat title")
    is_group: bool = Field(description="Group chat")
    message_count: int = Field(description="Messages in the chat")
    last_message_at: datetime | None = Field(description="Latest message")
    first_message_at: datetime | None = Field(description="Earliest message")
    last_text: str = Field(description="The most recent message text")


class ChatMessage(FrozenModel):
    """One archived message in a text conversation."""

    timestamp: datetime = Field(description="When the message was sent")
    sender_name: str = Field(description="Name shown for the sender")
    is_sender: bool = Field(description="Whether the user sent it")
    text: str = Field(description="Archived message text, possibly empty for attachments")


class DossierText(FrozenModel):
    """The model-written sections, ready to render."""

    one_liner: str = Field(description="Who they are to the user")
    key_facts: tuple[str, ...] = Field(description="Bullets")
    how_we_met: tuple[str, ...] = Field(description="Bullets")
    what_they_care_about: tuple[str, ...] = Field(description="Bullets")
    where_things_stand: tuple[str, ...] = Field(description="Bullets")
    watch_out: tuple[str, ...] = Field(description="Bullets; may be empty")
    next_steps: tuple[str, ...] = Field(description="Bullets; may be empty")
    interests: tuple[str, ...] = Field(description="Topic tags")
    background: tuple[str, ...] = Field(description="Role tags")
    notes: str = Field(description="Free text the user typed on the page")
    edited: frozenset[str] = Field(description="Fields whose text is the user's, not the model's")
    written_on: datetime | None = Field(description="When the model wrote it; None when only edits exist")
    excerpt_count: int = Field(description="How many dated excerpts it drew on")


class PersonDetail(FrozenModel):
    """Everything the person page shows."""

    summary: PersonSummary = Field(description="Header facts")
    aliases: tuple[str, ...] = Field(description="Other names")
    emails: tuple[str, ...] = Field(description="All emails")
    phones: tuple[str, ...] = Field(description="All phones")
    first_contact: datetime | None = Field(description="Earliest interaction")
    received_automated_count: int = Field(description="Automated mail from them")
    briefing: str = Field(description="One paragraph on where the relationship stands, derived from the counts")
    dossier: DossierText | None = Field(description="Model-written sections, when they exist")
    email_items: tuple[TimelineItem, ...] = Field(description="Human emails, newest first")
    bulk_email_count: int = Field(description="Automated emails left out of the list")
    meeting_series: tuple[MeetingSeries, ...] = Field(description="Meetings grouped by title, latest first")
    chats: tuple[ChatSummary, ...] = Field(description="Text conversations, most recent first")
    activity: tuple[YearActivity, ...] = Field(description="Per-year counts, oldest first")
    merge_candidates: tuple[PersonSummary, ...] = Field(description="People who might be the same person")
    social_profiles: tuple[SocialProfileRow, ...] = Field(default=(), description="Verified public social profiles and reach")


class PersonLink(FrozenModel):
    """A name that leads to a person's page."""

    name: str = Field(description="Display name")
    person_id: str = Field(description="Stable id")


class SearchHit(FrozenModel):
    """One hit of the cross-source search."""

    kind: str = Field(description="'email' or 'meeting'")
    timestamp: datetime = Field(description="When")
    direction: str = Field(description="For emails")
    title: str = Field(description="Subject or event title")
    snippet_html: str = Field(description="Excerpt as safe HTML, matches wrapped in <mark>")
    who: str = Field(description="Sender name or address")
    who_person_id: str = Field(description="Resolved person id of the sender, if any")
    people: tuple[PersonLink, ...] = Field(default=(), description="Everyone on the record who resolves to a person, the sender first")
    account: str = Field(description="Account")
    source_id: str = Field(description="Message or event id")
    thread_id: str = Field(description="Gmail thread id, when an email")


class RawRecordView(FrozenModel):
    """A raw archived record and its projection, for the 'show original' page."""

    source: str = Field(description="Source kind")
    account: str = Field(description="Account")
    record_id: str = Field(description="Id")
    raw_json: str = Field(description="Pretty-printed raw record")
    text: str = Field(description="Projected body text when a message")
    title: str = Field(description="Subject or summary")


class MergeCandidatePair(FrozenModel):
    """Two people proposed as the same person."""

    person_a: PersonSummary = Field(description="One")
    person_b: PersonSummary = Field(description="The other")
    reason: str = Field(description="Why they were proposed")
    lean: str = Field(default="", description="The judge's verdict when it looked: same / different / unsure, else empty")
    lean_confidence: float = Field(default=0.0, description="The judge's confidence")
    lean_why: str = Field(default="", description="The judge's reason")


class MergeReview(FrozenModel):
    """The duplicates page: what is left for the user, and what the judge already settled."""

    pairs: tuple[MergeCandidatePair, ...] = Field(description="Undecided pairs, the judge's lean attached where it has one")
    judged_same: tuple[MergeJudgmentRow, ...] = Field(description="Pairs the judge joined, newest first")
    judged_different: tuple[MergeJudgmentRow, ...] = Field(description="Pairs the judge kept apart, newest first")


# The search index marks matches with [ ]; turn them into <mark> only after escaping the text.
_MATCH_OPEN: Final[str] = "["
_MATCH_CLOSE: Final[str] = "]"


@pure
def _bullets(value: str) -> tuple[str, ...]:
    return tuple(line.strip().lstrip("-").strip() for line in value.splitlines() if line.strip())


@pure
def _tags_from_json(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


@pure
def _highlighted_html(snippet: str) -> str:
    return str(escape(snippet)).replace(_MATCH_OPEN, "<mark>").replace(_MATCH_CLOSE, "</mark>")


@pure
def _from_epoch(value: Any) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(int(value), tz=timezone.utc)


_PERSON_COLUMNS: Final[str] = (
    "person_id, canonical_name, emails_json, organization, title, tier, is_colleague, score, "
    "sent_count, received_human_count, meeting_count, last_contact_ts, first_contact_ts, hand_kept_category, "
    "phones_json, text_days_sent, text_days_received"
)
TRACE_MONTHS: Final[int] = 24
HEATMAP_YEARS: Final[int] = 8


@pure
def _summary_from_row(
    row: tuple[Any, ...], heatmap: tuple[YearRow, ...], roles: tuple[str, ...] = (), interests: tuple[str, ...] = (), thread_note: str = ""
) -> PersonSummary:
    emails = json.loads(row[2])
    peak = max((count for year in heatmap for count in year.months), default=0)
    trace = tuple(count for year in reversed(heatmap[:2]) for count in year.months)[-TRACE_MONTHS:] if heatmap else ()
    return PersonSummary(
        person_id=str(row[0]), name=str(row[1]), primary_email=str(emails[0]) if emails else "",
        organization=str(row[3]), title=str(row[4]), tier=int(row[5]), is_colleague=bool(row[6]),
        score=int(row[7]), sent_count=int(row[8]), received_count=int(row[9]), meeting_count=int(row[10]),
        last_contact=_from_epoch(row[11]), first_contact=_from_epoch(row[12]), hand_kept_category=str(row[13]),
        phones=tuple(str(p) for p in json.loads(row[14])), text_days=int(row[15]) + int(row[16]),
        trace=trace, heatmap=heatmap, heatmap_peak=peak, roles=roles, interests=interests, thread_note=thread_note,
    )


def _heatmaps_for(store: ArchiveStore, rows: Sequence[tuple[Any, ...]]) -> dict[str, tuple[YearRow, ...]]:
    """Per person, non-bulk emails plus meetings plus texting days per month, for each of the last HEATMAP_YEARS years.

    Read from the person_months table the identity pass keeps, so a page of people costs one small query."""
    now = datetime.now(timezone.utc)
    first_year = now.year - HEATMAP_YEARS + 1
    grid: dict[str, list[list[int]]] = {str(row[0]): [[0] * 12 for _ in range(HEATMAP_YEARS)] for row in rows}
    person_ids = list(grid)
    if person_ids:
        placeholders = ",".join("?" for _ in person_ids)
        for pid, month_key, emails, meetings, text_days in store.execute_read(
            f"SELECT person_id, month, emails, meetings, text_days FROM person_months WHERE person_id IN ({placeholders}) AND month >= ? AND month <= ?",
            [*person_ids, f"{first_year}-01", now.strftime("%Y-%m")],
        ):
            grid[str(pid)][int(month_key[:4]) - first_year][int(month_key[5:]) - 1] += int(emails) + int(meetings) + int(text_days)
    # Newest year on top: the current year is the first row.
    return {
        pid: tuple(YearRow(year=first_year + idx, months=tuple(months)) for idx, months in reversed(list(enumerate(years))))
        for pid, years in grid.items()
    }


def list_people(store: ArchiveStore, filters: PeopleListFilters) -> PeopleListPage:
    where = ["is_human = 1"]
    parameters: list[Any] = []
    if filters.group == "colleagues":
        where.append("is_colleague = 1")
    elif filters.group == "network":
        where.append("is_colleague = 0")
    else:
        pass
    if filters.tier is not None:
        where.append("tier = ?")
        parameters.append(filters.tier)
    if filters.query:
        like = f"%{filters.query}%"
        where.append("(canonical_name LIKE ? OR emails_json LIKE ? OR organization LIKE ? OR aliases_json LIKE ?)")
        parameters.extend([like, like, like, like])
    where_sql = " AND ".join(where)
    order_sql = "last_contact_ts DESC NULLS LAST, score DESC" if filters.sort == "recent" else "score DESC, last_contact_ts DESC"
    grouped = filters.group_by in GROUP_BY_OPTIONS
    limit, offset = (GROUPED_PEOPLE_LIMIT, 0) if grouped else (PEOPLE_PAGE_SIZE, filters.page * PEOPLE_PAGE_SIZE)
    rows = store.execute_read(
        f"SELECT {_PERSON_COLUMNS} FROM persons WHERE {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?", [*parameters, limit, offset]
    )
    total = store.execute_read(f"SELECT COUNT(*) FROM persons WHERE {where_sql}", parameters)[0][0]
    # One pass over the list index gives every count the header shows.
    count_rows = store.execute_read("SELECT is_colleague, tier, COUNT(*) FROM persons WHERE is_human=1 GROUP BY is_colleague, tier", [])
    human_count = sum(int(count) for is_colleague, _tier, count in count_rows if not is_colleague)
    colleague_count = sum(int(count) for is_colleague, _tier, count in count_rows if is_colleague)
    tier_counts: dict[int, int] = {}
    for is_colleague, tier, count in count_rows:
        if not is_colleague:
            tier_counts[int(tier)] = tier_counts.get(int(tier), 0) + int(count)
    tags = _tags_for(store, [str(row[0]) for row in rows])
    notes = _thread_notes_for(store, [str(row[0]) for row in rows])
    specs: tuple[_GroupSpec, ...] = ()
    looked_at = len(rows)
    if grouped:
        specs = _group_specs(rows, tags, filters.group_by, filters.group_value)
        shown = {pid for spec in specs for pid in spec.person_ids}
        rows = [row for row in rows if str(row[0]) in shown]
    heatmaps = _heatmaps_for(store, rows)
    summaries = {
        str(row[0]): _summary_from_row(row, heatmaps[str(row[0])], *tags.get(str(row[0]), ((), ())), thread_note=notes.get(str(row[0]), ""))
        for row in rows
    }
    groups = tuple(
        PeopleGroup(key=spec.key, label=spec.label, count=spec.count, people=tuple(summaries[pid] for pid in spec.person_ids)) for spec in specs
    )
    return PeopleListPage(
        people=tuple(summaries.values()) if not grouped else (), total_count=int(total),
        human_count=int(human_count), colleague_count=int(colleague_count),
        tier_counts=tier_counts, groups=groups, grouped_count=looked_at if grouped else 0,
    )


class _GroupSpec(FrozenModel):
    """A group before its people are turned into summaries."""

    key: str = Field(description="Stored value")
    label: str = Field(description="Heading")
    count: int = Field(description="Members")
    person_ids: tuple[str, ...] = Field(description="Members shown, in list order")


def _tags_for(store: ArchiveStore, person_ids: Sequence[str]) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """(roles, interests) per person: the user's edit wins, then the written dossier, then the cheap tagger."""
    if not person_ids:
        return {}
    placeholders = ",".join("?" for _ in person_ids)
    roles: dict[str, tuple[str, ...]] = {}
    interests: dict[str, tuple[str, ...]] = {}
    for pid, background_json, interests_json in store.execute_read(
        f"SELECT person_id, background_json, interests_json FROM person_tags WHERE person_id IN ({placeholders})", list(person_ids)
    ):
        roles[str(pid)] = tuple(_tags_from_json(str(background_json)))
        interests[str(pid)] = tuple(_tags_from_json(str(interests_json)))
    for pid, background_json, interests_json in store.execute_read(
        f"SELECT person_id, background_json, interests_json FROM person_synthesis WHERE person_id IN ({placeholders})", list(person_ids)
    ):
        roles[str(pid)] = tuple(_tags_from_json(str(background_json)))
        interests[str(pid)] = tuple(_tags_from_json(str(interests_json)))
    for pid, field, value in store.execute_read(
        f"SELECT person_id, field, value FROM person_edits WHERE field IN ('background', 'interests') AND person_id IN ({placeholders})", list(person_ids)
    ):
        (roles if field == "background" else interests)[str(pid)] = parse_tag_list(str(value))
    return {pid: (roles.get(pid, ()), interests.get(pid, ())) for pid in person_ids if pid in roles or pid in interests}


# Thread verdicts worth showing: someone the user knows, or a thread the judge could not place.
_SHOWN_THREAD_KINDS: Final[frozenset[str]] = frozenset({"warm", "unsure"})


def _thread_notes_for(store: ArchiveStore, person_ids: Sequence[str]) -> dict[str, str]:
    """Per person, the one-line description the thread judge wrote about the number behind them."""
    if not person_ids:
        return {}
    placeholders = ",".join("?" for _ in person_ids)
    notes: dict[str, str] = {}
    for pid, kind, who in store.execute_read(
        "SELECT h.person_id, t.kind, t.who FROM person_handles h JOIN thread_judgments t ON t.handle_key = h.handle_key "
        f"WHERE h.person_id IN ({placeholders}) ORDER BY t.confidence DESC",
        list(person_ids),
    ):
        if str(kind) in _SHOWN_THREAD_KINDS and str(who) and str(pid) not in notes:
            notes[str(pid)] = str(who)
    return notes


# The group a person lands in when the chosen axis says nothing about them.
_UNGROUPED_LABEL: Final[dict[str, str]] = {
    "role": "No role yet", "interest": "No interests yet", "organization": "No organization", "category": "Not in your contacts file",
}


@pure
def _group_specs(rows: Sequence[tuple[Any, ...]], tags: dict[str, tuple[tuple[str, ...], tuple[str, ...]]], axis: str, only: str) -> tuple[_GroupSpec, ...]:
    """Bucket the rows along one axis (a person with several tags lands in each), biggest bucket first, the empty bucket last."""
    members: dict[str, list[str]] = {}
    for row in rows:
        pid = str(row[0])
        if axis == "role":
            keys = tags.get(pid, ((), ()))[0]
        elif axis == "interest":
            keys = tags.get(pid, ((), ()))[1]
        elif axis == "organization":
            keys = (str(row[3]),) if row[3] else ()
        else:
            keys = (str(row[13]),) if row[13] else ()
        for key in keys or ("",):
            members.setdefault(key, []).append(pid)
    if only:
        chosen = members.get(only, [])
        return (_GroupSpec(key=only, label=only or _UNGROUPED_LABEL[axis], count=len(chosen), person_ids=tuple(chosen)),) if chosen else ()
    ordered = sorted(members.items(), key=lambda item: (item[0] == "", -len(item[1]), item[0]))
    return tuple(
        _GroupSpec(key=key, label=key or _UNGROUPED_LABEL[axis], count=len(pids), person_ids=tuple(pids[:GROUP_PREVIEW_SIZE])) for key, pids in ordered
    )


def get_person(store: ArchiveStore, person_id: str) -> PersonDetail | None:
    rows = store.execute_read(
        f"SELECT {_PERSON_COLUMNS}, aliases_json, phones_json, first_contact_ts, received_automated_count "
        "FROM persons WHERE person_id = ?",
        [person_id],
    )
    if not rows:
        return None
    row = rows[0]
    # The extra columns start right after the shared person columns.
    extra = len(_PERSON_COLUMNS.split(", "))
    summary = _summary_from_row(row, _heatmaps_for(store, [row])[str(row[0])], thread_note=_thread_notes_for(store, [person_id]).get(person_id, ""))
    emails = tuple(str(e) for e in json.loads(row[2]))
    email_items, bulk_count = _emails_for(store, emails)
    series = _meeting_series_for(store, emails)
    chats = _chats_for(store, person_id)
    candidate_rows = store.execute_read(
        "SELECT CASE WHEN person_id_a = ? THEN person_id_b ELSE person_id_a END FROM merge_candidates "
        "WHERE person_id_a = ? OR person_id_b = ?",
        [person_id, person_id, person_id],
    )
    id_pairs = [(person_id, str(r[0])) for r in candidate_rows]
    undecided = _undecided_pairs(id_pairs, _handles_by_person(store, id_pairs))
    candidates = [c for c in (_summary_by_id(store, other) for _me, other in undecided) if c is not None]
    activity = _activity_by_year(email_items, series)
    return PersonDetail(
        dossier=_dossier_for(store, person_id),
        summary=summary, aliases=tuple(str(a) for a in json.loads(row[extra])), emails=emails,
        briefing=_briefing(summary, _from_epoch(row[extra + 2]), activity, series),
        phones=tuple(str(p) for p in json.loads(row[extra + 1])), first_contact=_from_epoch(row[extra + 2]),
        received_automated_count=int(row[extra + 3]), email_items=email_items, bulk_email_count=bulk_count,
        meeting_series=series, chats=chats, activity=activity, merge_candidates=tuple(candidates),
        social_profiles=store.social_profiles(PersonId(person_id)),
    )


def _dossier_for(store: ArchiveStore, person_id: str) -> DossierText | None:
    """The model's sections with the user's edits laid over them; None when neither exists."""
    synthesis = store.get_synthesis(PersonId(person_id))
    edits = store.person_edits(PersonId(person_id))
    if synthesis is None and not edits:
        return None
    text: dict[str, str] = {field: "" for field in DOSSIER_FIELDS}
    if synthesis is not None:
        text.update(
            one_liner=synthesis.one_liner, key_facts=synthesis.key_facts, how_we_met=synthesis.how_we_met,
            what_they_care_about=synthesis.what_they_care_about, where_things_stand=synthesis.where_things_stand,
            watch_out=synthesis.watch_out, interests=", ".join(_tags_from_json(synthesis.interests_json)),
            background=", ".join(_tags_from_json(synthesis.background_json)),
        )
    text.update(edits)
    return DossierText(
        one_liner=text["one_liner"], key_facts=_bullets(text["key_facts"]), how_we_met=_bullets(text["how_we_met"]),
        what_they_care_about=_bullets(text["what_they_care_about"]), where_things_stand=_bullets(text["where_things_stand"]),
        watch_out=_bullets(text["watch_out"]), next_steps=_bullets(synthesis.next_steps) if synthesis is not None else (),
        interests=parse_tag_list(text["interests"]), background=parse_tag_list(text["background"]), notes=text["notes"],
        edited=frozenset(edits), written_on=synthesis.synthesized_at if synthesis is not None else None,
        excerpt_count=synthesis.excerpt_count if synthesis is not None else 0,
    )


def _summary_by_id(store: ArchiveStore, person_id: str) -> PersonSummary | None:
    rows = store.execute_read(f"SELECT {_PERSON_COLUMNS} FROM persons WHERE person_id = ?", [person_id])
    return _summary_from_row(rows[0], _heatmaps_for(store, rows)[str(rows[0][0])]) if rows else None


def _emails_for(store: ArchiveStore, emails: tuple[str, ...]) -> tuple[tuple[TimelineItem, ...], int]:
    """Human emails involving these addresses, newest first, and how many bulk ones were left out."""
    if not emails:
        return (), 0
    placeholders = ",".join("?" for _ in emails)
    # Pick the newest messages from the participants index first, then read only those: reading
    # every message a well-connected person was ever on took seconds per page.
    rows = store.execute_read(
        "SELECT m.ts, m.direction, m.subject, m.snippet, m.account, m.message_id, m.thread_id, m.is_automated "
        "FROM (SELECT account, message_id, MAX(ts) AS ts FROM message_participants "
        f"      WHERE address IN ({placeholders}) GROUP BY account, message_id ORDER BY ts DESC LIMIT ?) p "
        "JOIN messages m ON m.account = p.account AND m.message_id = p.message_id ORDER BY m.ts DESC",
        [*emails, TIMELINE_LIMIT],
    )
    items = [
        TimelineItem(
            kind="email", timestamp=_from_epoch(r[0]) or datetime.now(timezone.utc), direction=str(r[1]),
            title=str(r[2]) or "(no subject)", snippet=str(r[3]), account=str(r[4]), source_id=str(r[5]),
            thread_id=str(r[6]), is_automated=bool(r[7]),
        )
        for r in rows
    ]
    human = tuple(item for item in items if not item.is_automated)
    return human, len(items) - len(human)


@pure
def _address_phrases(emails: Sequence[str]) -> str:
    """A search-index query matching any of these addresses: each one as a phrase of its word parts."""
    phrases = []
    for email in emails:
        words = [w for w in re.split(r"[^0-9A-Za-z]+", email) if w]
        if words:
            phrases.append('"' + " ".join(words) + '"')
    return " OR ".join(phrases) or '""'


def _meeting_series_for(store: ArchiveStore, emails: tuple[str, ...]) -> tuple[MeetingSeries, ...]:
    """Calendar events with these addresses on the invite, collapsed by title, latest first."""
    if not emails:
        return ()
    # The search index narrows the events to ones mentioning the address's words; the LIKE then
    # confirms the exact address, so a page no longer scans every calendar event.
    like_clauses = " OR ".join("attendees_json LIKE ?" for _ in emails)
    rows = store.execute_read(
        f"SELECT summary, COUNT(*), MIN(start_ts), MAX(start_ts), account, MAX(event_id) FROM calendar_events "
        "WHERE rowid IN (SELECT rowid FROM calendar_events_fts WHERE calendar_events_fts MATCH ?) "
        f"AND start_ts <= ? AND ({like_clauses}) GROUP BY summary, account ORDER BY MAX(start_ts) DESC",
        [_address_phrases(emails), int(datetime.now(timezone.utc).timestamp()), *[f'%"{e}"%' for e in emails]],
    )
    return tuple(
        MeetingSeries(
            title=str(r[0]) or "(untitled event)", count=int(r[1]), first=_from_epoch(r[2]) or datetime.now(timezone.utc),
            last=_from_epoch(r[3]) or datetime.now(timezone.utc), account=str(r[4]), latest_event_id=str(r[5]),
        )
        for r in rows
    )


@pure
def _briefing(summary: PersonSummary, first: datetime | None, activity: tuple["YearActivity", ...], series: tuple["MeetingSeries", ...]) -> str:
    """Where you stand with this person, in plain words, from the numbers alone."""
    if first is None or summary.last_contact is None:
        return "No dated interactions archived yet."
    span_years = max(1, summary.last_contact.year - first.year + 1)
    since = f"since {first.year}" if first.year != summary.last_contact.year else f"in {first.year}"
    balance = ("mostly you writing to them" if summary.sent_count > 2 * max(1, summary.received_count)
               else "mostly them writing to you" if summary.received_count > 2 * max(1, summary.sent_count)
               else "a two-way exchange")
    total_meetings = sum(one.count for one in series)
    recurring = [one for one in series if one.count >= 3]
    parts = [f"In touch {since} across {span_years} year{'s' if span_years > 1 else ''}."]
    if summary.sent_count or summary.received_count:
        parts.append(f"{summary.sent_count + summary.received_count} emails, {balance}.")
    else:
        parts.append("No direct email between you.")
    if total_meetings:
        recurring_note = f", including {len(recurring)} recurring" if recurring else ""
        parts.append(f"{total_meetings} meetings on the calendar{recurring_note}.")
    days_quiet = (datetime.now(timezone.utc) - summary.last_contact).days
    if days_quiet > 365:
        parts.append(f"Quiet for {days_quiet // 365} year{'s' if days_quiet >= 730 else ''} — last contact {summary.last_contact:%B %Y}.")
    elif days_quiet > 60:
        parts.append(f"Last contact {summary.last_contact:%B %Y}.")
    else:
        parts.append("Active recently.")
    if summary.hand_kept_category:
        parts.append(f"In your contacts file as “{summary.hand_kept_category}”.")
    return " ".join(parts)


@pure
def _activity_by_year(email_items: tuple[TimelineItem, ...], series: tuple[MeetingSeries, ...]) -> tuple[YearActivity, ...]:
    sent: dict[int, int] = {}
    received: dict[int, int] = {}
    meetings: dict[int, int] = {}
    for item in email_items:
        bucket = sent if item.direction == "SENT" else received
        bucket[item.timestamp.year] = bucket.get(item.timestamp.year, 0) + 1
    # A series spans first..last; spread its occurrences evenly across those years.
    for one in series:
        years = list(range(one.first.year, one.last.year + 1))
        for year in years:
            meetings[year] = meetings.get(year, 0) + max(1, one.count // len(years))
    all_years = sorted(set(sent) | set(received) | set(meetings))
    return tuple(YearActivity(year=y, sent=sent.get(y, 0), received=received.get(y, 0), meetings=meetings.get(y, 0)) for y in all_years)


def _chats_for(store: ArchiveStore, person_id: str) -> tuple[ChatSummary, ...]:
    rows = store.execute_read(
        "SELECT c.chat_id, c.network, c.title, c.chat_type, COUNT(m.message_id), MAX(m.ts), MIN(m.ts) "
        "FROM person_handles h JOIN beeper_participants p ON p.handle_key = h.handle_key AND p.is_self = 0 "
        "JOIN beeper_chats c ON c.chat_id = p.chat_id LEFT JOIN beeper_messages m ON m.chat_id = c.chat_id "
        "WHERE h.person_id = ? GROUP BY c.chat_id ORDER BY MAX(m.ts) DESC",
        [person_id],
    )
    chats: list[ChatSummary] = []
    for chat_id, network, title, chat_type, count, last_ts, first_ts in rows:
        last_text_rows = store.execute_read("SELECT text FROM beeper_messages WHERE chat_id = ? AND text != '' ORDER BY ts DESC LIMIT 1", [chat_id])
        chats.append(ChatSummary(
            chat_id=str(chat_id), network=str(network), title=str(title), is_group=str(chat_type) == "group",
            message_count=int(count), last_message_at=_from_epoch(last_ts), first_message_at=_from_epoch(first_ts),
            last_text=str(last_text_rows[0][0])[:160] if last_text_rows else "",
        ))
    return tuple(chats)


def chat_messages_for(
    store: ArchiveStore, person_id: str, chat_id: str
) -> tuple[ChatMessage, ...] | None:
    """Return the full chat only when it belongs to the requested person."""
    belongs = store.execute_read(
        "SELECT 1 FROM person_handles h JOIN beeper_participants p "
        "ON p.handle_key=h.handle_key AND p.is_self=0 "
        "WHERE h.person_id=? AND p.chat_id=? LIMIT 1",
        [person_id, chat_id],
    )
    if not belongs:
        return None
    rows = store.execute_read(
        "SELECT ts, sender_name, is_sender, text FROM beeper_messages "
        "WHERE chat_id=? ORDER BY ts, message_id",
        [chat_id],
    )
    return tuple(
        ChatMessage(
            timestamp=datetime.fromtimestamp(int(ts), timezone.utc),
            sender_name=str(sender_name),
            is_sender=bool(is_sender),
            text=str(message_text),
        )
        for ts, sender_name, is_sender, message_text in rows
    )


def handles_of(store: ArchiveStore, person_id: str) -> list[str]:
    """Every handle key the person currently holds, for recording a decision that survives rebuilds."""
    return [str(row[0]) for row in store.execute_read("SELECT handle_key FROM person_handles WHERE person_id = ?", [person_id])]


def _person_id_for_handle(store: ArchiveStore, handle_key: str) -> str:
    rows = store.execute_read("SELECT person_id FROM person_handles WHERE handle_key = ?", [handle_key])
    return str(rows[0][0]) if rows else ""


def _person_id_for_address(store: ArchiveStore, address: str) -> str:
    rows = store.execute_read("SELECT person_id FROM person_handles WHERE handle_key = ?", [f"email:{address}"])
    return str(rows[0][0]) if rows else ""


# How many linked people a search hit shows before "+N".
SEARCH_HIT_PEOPLE: Final[int] = 4


def search_everything(store: ArchiveStore, query: str) -> list[SearchHit]:
    emails = store.search_messages(query, SEARCH_LIMIT)
    texts = store.search_texts(query, SEARCH_LIMIT)
    events = store.search_events(query, SEARCH_LIMIT)
    handle_keys = {
        *(f"email:{address}" for hit in emails for address in (hit.from_addr, *hit.recipient_addresses) if address),
        *(key for text in texts for key in (text.sender_handle_key, *text.other_handle_keys) if key),
        *(f"email:{address}" for event in events for address in event.attendee_addresses),
    }
    link_by_handle = _person_links_for(store, sorted(handle_keys))

    def links(keys: Sequence[str]) -> tuple[PersonLink, ...]:
        seen: list[PersonLink] = []
        for key in keys:
            link = link_by_handle.get(key)
            if link is not None and link not in seen:
                seen.append(link)
        return tuple(seen)

    hits: list[SearchHit] = []
    for hit in emails:
        sender = link_by_handle.get(f"email:{hit.from_addr}") if hit.from_addr else None
        hits.append(SearchHit(
            kind="email", timestamp=hit.timestamp, direction=hit.direction.value, title=hit.subject or "(no subject)",
            snippet_html=_highlighted_html(hit.snippet), who=hit.from_name or hit.from_addr,
            who_person_id=sender.person_id if sender is not None else "",
            people=links([f"email:{hit.from_addr}", *(f"email:{address}" for address in hit.recipient_addresses)]),
            account=str(hit.account), source_id=str(hit.message_id), thread_id=str(hit.thread_id),
        ))
    for text in texts:
        sender = link_by_handle.get(text.sender_handle_key)
        hits.append(SearchHit(
            kind="text", timestamp=text.timestamp, direction="SENT" if text.is_sender else "RECEIVED",
            title=f"{text.network} · {text.chat_title}", snippet_html=_highlighted_html(text.snippet),
            who="you" if text.is_sender else text.sender_name, who_person_id=sender.person_id if sender is not None else "",
            people=links([text.sender_handle_key, *text.other_handle_keys]), account="beeper@local",
            source_id=text.message_id, thread_id=text.chat_id,
        ))
    for event in events:
        hits.append(SearchHit(
            kind="meeting", timestamp=event.start, direction="OBSERVED", title=event.summary or "(untitled event)",
            snippet_html=_highlighted_html(f"{event.attendee_count} on the invite"), who="", who_person_id="",
            people=links([f"email:{address}" for address in event.attendee_addresses]),
            account=str(event.account), source_id=str(event.event_id), thread_id="",
        ))
    return sorted(hits, key=lambda h: h.timestamp, reverse=True)


def _person_links_for(store: ArchiveStore, handle_keys: Sequence[str]) -> dict[str, PersonLink]:
    """Handle key to the human it resolves to, in one query; the user and non-people are left out."""
    if not handle_keys:
        return {}
    placeholders = ",".join("?" for _ in handle_keys)
    rows = store.execute_read(
        "SELECT h.handle_key, p.person_id, p.canonical_name FROM person_handles h JOIN persons p ON p.person_id = h.person_id "
        f"WHERE p.is_human = 1 AND h.handle_key IN ({placeholders}) "
        "AND p.person_id NOT IN (SELECT o.person_id FROM person_handles o JOIN (SELECT DISTINCT account FROM messages) a ON o.handle_key = 'email:' || a.account)",
        list(handle_keys),
    )
    return {str(key): PersonLink(name=str(name), person_id=str(pid)) for key, pid, name in rows}


def get_raw_record(store: ArchiveStore, source: str, account: str, record_id: str) -> RawRecordView | None:
    kind = SourceKind(source)
    raw = store.get_raw_record(kind, AccountEmail(account), record_id)
    if raw is None:
        return None
    text = ""
    title = ""
    if kind == SourceKind.GMAIL:
        rows = store.execute_read("SELECT subject, text FROM messages WHERE account = ? AND message_id = ?", [account, record_id])
        if rows:
            title, text = str(rows[0][0]), str(rows[0][1])
    elif kind == SourceKind.GOOGLE_CALENDAR:
        title = str(raw.get("summary", ""))
    else:
        title = str(raw.get("names", [{}])[0].get("displayName", "")) if raw.get("names") else ""
    return RawRecordView(
        source=source, account=account, record_id=record_id, raw_json=json.dumps(raw, indent=2, ensure_ascii=False),
        text=text, title=title,
    )


def list_merge_candidates(store: ArchiveStore) -> MergeReview:
    """Candidate pairs among people, both sides in one query; no activity traces (the page lists hundreds).

    Undecided pairs carry the judge's lean when it has looked; the pairs it settled are listed separately
    so the user can see what was done for the user's.
    """
    rows = store.execute_read(
        f"SELECT c.reason, {', '.join('a.' + col for col in _PERSON_COLUMNS.split(', '))}, "
        f"{', '.join('b.' + col for col in _PERSON_COLUMNS.split(', '))} "
        "FROM merge_candidates c JOIN persons a ON a.person_id = c.person_id_a JOIN persons b ON b.person_id = c.person_id_b "
        "WHERE a.is_human = 1 AND b.is_human = 1",
        [],
    )
    width = len(_PERSON_COLUMNS.split(", "))
    id_pairs = [(str(row[1]), str(row[1 + width])) for row in rows]
    handles = _handles_by_person(store, id_pairs)
    undecided = set(_undecided_pairs(id_pairs, handles))
    judgments = store.merge_judgments()
    pairs = []
    for row in rows:
        id_pair = (str(row[1]), str(row[1 + width]))
        if id_pair not in undecided:
            continue
        judgment = _judgment_for(judgments, handles[id_pair[0]], handles[id_pair[1]])
        pairs.append(MergeCandidatePair(
            person_a=_summary_from_row(row[1:1 + width], ()),
            person_b=_summary_from_row(row[1 + width:1 + 2 * width], ()),
            reason=str(row[0]),
            lean=judgment.verdict if judgment else "", lean_confidence=judgment.confidence if judgment else 0.0,
            lean_why=judgment.reason if judgment else "",
        ))
    # Unsure pairs first: they are the ones that need the user's; then whatever the judge has not reached.
    order = {"unsure": 0, "": 1, "same": 2, "different": 3}
    pairs.sort(key=lambda pair: (order[pair.lean], -(pair.person_a.score + pair.person_b.score)))
    settled = sorted((j for j in judgments if decision_for(j) is not None), key=lambda j: j.judged_at, reverse=True)
    return MergeReview(
        pairs=tuple(pairs),
        judged_same=tuple(j for j in settled if j.verdict == "same"),
        judged_different=tuple(j for j in settled if j.verdict == "different"),
    )


@pure
def _judgment_for(judgments: Sequence[MergeJudgmentRow], handles_a: Sequence[str], handles_b: Sequence[str]) -> MergeJudgmentRow | None:
    side_a = set(handles_a)
    side_b = set(handles_b)
    for judgment in judgments:
        judged_a = set(judgment.handles_a)
        judged_b = set(judgment.handles_b)
        if (side_a & judged_a and side_b & judged_b) or (side_a & judged_b and side_b & judged_a):
            return judgment
    return None


def _handles_by_person(store: ArchiveStore, pairs: Sequence[tuple[str, str]]) -> dict[str, list[str]]:
    person_ids = sorted({person_id for pair in pairs for person_id in pair})
    handles: dict[str, list[str]] = {person_id: [] for person_id in person_ids}
    if not person_ids:
        return handles
    for row in store.execute_read(
        f"SELECT person_id, handle_key FROM person_handles WHERE person_id IN ({', '.join('?' * len(person_ids))})", person_ids,
    ):
        handles[str(row[0])].append(str(row[1]))
    return handles


def _undecided_pairs(pairs: Sequence[tuple[str, str]], handles: Mapping[str, Sequence[str]]) -> list[tuple[str, str]]:
    """The pairs neither the user nor the judge has ruled on.

    The candidate table is only rebuilt by the background identity pass, which takes minutes; the decision
    ledgers are current the moment a decision lands, so a decided pair leaves the page at once.
    """
    if not pairs:
        return []
    decided = [*load_assertions(assertions_path()), *load_assertions(auto_decisions_path())]
    if not decided:
        return list(pairs)
    return [
        (person_a, person_b) for person_a, person_b in pairs
        if not any(is_decided(assertion, handles[person_a], handles[person_b]) for assertion in decided)
    ]



LAUNCH_LIST_NAME: Final[str] = "outreach"
# Group order = priority: a person lands in the first group they qualify for.
LAUNCH_GROUPS: Final[tuple[tuple[str, str], ...]] = (
    ("press", "Press and writers"),
    ("reach", "People with reach"),
    ("builders", "Founders, investors, operators"),
    ("makers", "Engineers, designers, researchers"),
    ("users", "Likely to use it"),
    ("friends", "Friends worth telling"),
)


class LaunchCandidate(FrozenModel):
    """One person on the launch list with why they are there and the user's decision."""

    person_id: str = Field(description="Stable id")
    name: str = Field(description="Name")
    organization: str = Field(description="Company")
    primary_email: str = Field(description="Best email")
    x_username: str = Field(description="X handle, if known")
    group: str = Field(description="Group key")
    reach: str = Field(description="none / some / high")
    fit: str = Field(description="no / maybe / yes")
    why: str = Field(description="One-clause reason")
    interests: tuple[str, ...] = Field(description="Topic tags")
    background: tuple[str, ...] = Field(description="Role tags")
    tier: int = Field(description="1..3")
    last_contact: datetime | None = Field(description="Latest interaction")
    decision: str = Field(description="'' / include / exclude")


class LaunchList(FrozenModel):
    """The launch list grouped, with counts."""

    groups: tuple[tuple[str, str, tuple[LaunchCandidate, ...]], ...] = Field(description="(key, label, people) in priority order")
    included_count: int = Field(description="People marked include")
    candidate_count: int = Field(description="People proposed")
    tagged_count: int = Field(description="People tagged so far (the pass may still be running)")


@pure
def _launch_group(hand_kept_category: str, background: tuple[str, ...], reach: str, fit: str, social_followers: int) -> str:
    roles = set(background)
    if hand_kept_category == "journalist" or roles & {"journalist", "writer"}:
        return "press"
    if social_followers >= 5_000 or reach == "high" or (reach == "some" and fit != "no"):
        return "reach"
    if roles & {"founder", "investor", "operator", "executive"} and fit != "no":
        return "builders"
    if roles & {"engineer", "designer", "researcher", "scientist", "academic"} and fit != "no":
        return "makers"
    if fit == "yes":
        return "users"
    if hand_kept_category == "trusted-warm" and fit != "no":
        return "friends"
    return ""


def build_launch_list(store: ArchiveStore) -> LaunchList:
    rows = store.execute_read(
        "SELECT p.person_id, p.canonical_name, p.organization, p.emails_json, p.hand_kept_category, p.tier, p.last_contact_ts, "
        "COALESCE(t.interests_json, '[]'), COALESCE(t.background_json, '[]'), COALESCE(t.reach, 'none'), "
        "COALESCE(t.reach_why, ''), COALESCE(t.fit, 'maybe'), COALESCE(t.fit_why, ''), "
        "COALESCE((SELECT b.username FROM person_handles h JOIN beeper_participants b ON b.handle_key = h.handle_key "
        "          WHERE h.person_id = p.person_id AND h.handle_key LIKE 'x:%' AND b.username != '' LIMIT 1), ''), "
        "COALESCE(s.social_followers, 0) "
        "FROM persons p LEFT JOIN person_tags t ON t.person_id = p.person_id "
        "LEFT JOIN (SELECT person_id, SUM(COALESCE(follower_count, 0)) AS social_followers FROM social_profiles GROUP BY person_id) s "
        "ON s.person_id = p.person_id "
        "WHERE p.is_human = 1 AND p.is_colleague = 0 ORDER BY p.score DESC",
        [],
    )
    decisions = store.list_decisions(LAUNCH_LIST_NAME)
    by_group: dict[str, list[LaunchCandidate]] = {key: [] for key, _ in LAUNCH_GROUPS}
    for row in rows:
        background = tuple(_tags_from_json(str(row[8])))
        social_followers = int(row[14])
        group = _launch_group(str(row[4]), background, str(row[9]), str(row[11]), social_followers)
        decision = decisions.get(PersonId(str(row[0])), "")
        if not group and decision != "include":
            continue
        emails = json.loads(row[3])
        by_group[group or "users"].append(LaunchCandidate(
            person_id=str(row[0]), name=str(row[1]), organization=str(row[2]), primary_email=str(emails[0]) if emails else "",
            x_username=str(row[13]), group=group or "users", reach=str(row[9]), fit=str(row[11]),
            why=(f"{social_followers:,} followers across public profiles" if social_followers and group == "reach"
                 else str(row[10]) if group in ("press", "reach") else str(row[12])),
            interests=tuple(_tags_from_json(str(row[7]))), background=background, tier=int(row[5]),
            last_contact=_from_epoch(row[6]), decision=decision,
        ))
    groups = tuple((key, label, tuple(by_group[key])) for key, label in LAUNCH_GROUPS if by_group[key])
    tagged = store.execute_read("SELECT COUNT(*) FROM person_tags", [])[0][0]
    return LaunchList(
        groups=groups, included_count=sum(1 for d in decisions.values() if d == "include"),
        candidate_count=sum(len(people) for _, _, people in groups), tagged_count=int(tagged),
    )



class EvidenceItem(FrozenModel):
    """One record shown in the evidence side panel."""

    kind: str = Field(description="email / text / meeting / note")
    when: datetime | None = Field(description="Date")
    title: str = Field(description="Subject, chat title, meeting title, or note title")
    body: str = Field(description="Text to show")
    link: str = Field(description="Where the original lives (Gmail URL, or empty)")


def evidence_for_person(store: ArchiveStore, person_id: str, month: str, kind: str, limit: int) -> list[EvidenceItem]:
    """Records of one kind involving the person in a given month (YYYY-MM), or any month when month is ''."""
    rows = store.execute_read("SELECT emails_json, canonical_name FROM persons WHERE person_id = ?", [person_id])
    if not rows:
        return []
    emails = [str(e) for e in json.loads(rows[0][0])]
    name = str(rows[0][1])
    low, high = _month_bounds(month)
    items: list[EvidenceItem] = []
    if kind == "email" and emails:
        placeholders = ",".join("?" for _ in emails)
        for ts, direction, subject, text, account, thread_id in store.execute_read(
            "SELECT DISTINCT m.ts, m.direction, m.subject, m.text, m.account, m.thread_id FROM message_participants p "
            "JOIN messages m ON m.account = p.account AND m.message_id = p.message_id "
            f"WHERE m.is_automated = 0 AND p.address IN ({placeholders}) AND m.ts BETWEEN ? AND ? ORDER BY m.ts DESC LIMIT ?",
            [*emails, low, high, limit],
        ):
            who = "You wrote" if direction == "SENT" else f"{name} wrote"
            items.append(EvidenceItem(kind="email", when=_from_epoch(ts), title=f"{who}: {subject or '(no subject)'}", body=str(text)[:4000],
                                      link=f"https://mail.google.com/mail/u/0/?authuser={account}#all/{thread_id}"))
    elif kind == "text":
        for day, network, title, joined in store.execute_read(
            "SELECT m.ts / 86400 AS day, c.network, c.title, GROUP_CONCAT((CASE WHEN m.is_sender THEN 'You' ELSE m.sender_name END) || ': ' || m.text, '\n') "
            "FROM person_handles h JOIN beeper_participants p ON p.handle_key = h.handle_key AND p.is_self = 0 "
            "JOIN beeper_chats c ON c.chat_id = p.chat_id JOIN beeper_messages m ON m.chat_id = c.chat_id "
            "WHERE h.person_id = ? AND m.text != '' AND m.ts BETWEEN ? AND ? GROUP BY day, c.chat_id ORDER BY day DESC LIMIT ?",
            [person_id, low, high, limit],
        ):
            items.append(EvidenceItem(kind="text", when=_from_epoch(int(day) * 86400), title=f"{network} · {title}", body=str(joined)[:4000], link=""))
    elif kind == "meeting" and emails:
        like_clauses = " OR ".join("attendee_emails_json LIKE ?" for _ in emails)
        for ts, title, summary, transcript in store.execute_read(
            f"SELECT started_ts, title, summary, transcript FROM meetings WHERE ({like_clauses}) AND started_ts BETWEEN ? AND ? ORDER BY started_ts DESC LIMIT ?",
            [*[f'%"{e}"%' for e in emails], low, high, limit],
        ):
            items.append(EvidenceItem(kind="meeting", when=_from_epoch(ts), title=str(title), body=(str(summary) or str(transcript))[:6000], link=""))
    elif kind == "note":
        for ts, source, title, text in store.execute_read(
            "SELECT n.note_date_ts, n.source, n.title, n.text FROM note_mentions nm JOIN notes n ON n.source = nm.source AND n.path = nm.path "
            "WHERE nm.handle_key = ? AND COALESCE(n.note_date_ts, 0) BETWEEN ? AND ? ORDER BY COALESCE(n.note_date_ts, 0) DESC LIMIT ?",
            [f"note:{name.lower()}", low if month else 0, high, limit],
        ):
            items.append(EvidenceItem(kind="note", when=_from_epoch(ts) if ts else None, title=f"{source} · {title}", body=_lines_around(str(text), name)[:4000], link=""))
    else:
        pass
    return items


@pure
def _month_bounds(month: str) -> tuple[int, int]:
    """Epoch bounds of a YYYY-MM (or YYYY) string; the whole of time when empty."""
    if not month:
        return 0, 4102444800
    parts = month.split("-")
    year = int(parts[0])
    if len(parts) >= 2:
        first = datetime(year, int(parts[1]), 1, tzinfo=timezone.utc)
        last = datetime(year + (1 if int(parts[1]) == 12 else 0), 1 if int(parts[1]) == 12 else int(parts[1]) + 1, 1, tzinfo=timezone.utc)
    else:
        first = datetime(year, 1, 1, tzinfo=timezone.utc)
        last = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    return int(first.timestamp()), int(last.timestamp()) - 1


@pure
def _lines_around(text: str, name: str) -> str:
    needle = name.lower()
    lines = text.splitlines()
    picked: list[str] = []
    for idx, line in enumerate(lines):
        if needle in line.lower():
            picked.extend(l.strip() for l in lines[max(0, idx - 1): idx + 3])
            picked.append("…")
    return "\n".join(picked) if picked else text
