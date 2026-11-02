import json
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field, ValidationError

from people_archive.claude_p import ClaudeCLIError, claude_p_completion
from people_archive.errors import PeopleArchiveError
from people_archive.primitives import PersonId
from people_archive.settings import load_settings
from people_archive.store import ArchiveStore, PersonTagsRow

# Cheap, wide pass: a few lines of evidence per person, fifteen people per call.
TAGGER_MODEL: Final[str] = "claude-haiku-4-5"
PEOPLE_PER_CALL: Final[int] = 15
# Each call is a separate claude process (~300 MB); more than a few at once gets them killed for memory.
_WORKERS: Final[int] = 2
_SUBJECTS_PER_PERSON: Final[int] = 4
_NOTE_LINES_PER_PERSON: Final[int] = 3
_MEETING_TITLES_PER_PERSON: Final[int] = 3

_SYSTEM_PROMPT: Final[str] = """You tag people in the user's personal CRM from short evidence.
For each numbered entry, decide from the evidence only:

- interests: 2-6 short lowercase topic tags (e.g. "ai agents", "meditation", "urban design", "open source").
- background: 1-3 role tags from: engineer, designer, founder, investor, researcher, operator, writer, journalist, artist, scientist, executive, academic, other.
- reach: how much of an audience this person has. "high" = well-known writer/founder/investor/journalist or
  clearly large following; "some" = writes publicly, hosts events, or is a known name in a scene; "none" = no sign.
- reach_why: one short clause.
- fit: would this person plausibly be interested in the configured outreach topic? If no topic is supplied, say maybe. "yes", "maybe", or "no".
- fit_why: one short clause.

Return ONLY a JSON array, one object per entry, same order:
{"n": <number>, "interests": [...], "background": [...], "reach": "...", "reach_why": "...", "fit": "...", "fit_why": "..."}"""

_REACH_VALUES: Final[frozenset[str]] = frozenset({"none", "some", "high"})
_FIT_VALUES: Final[frozenset[str]] = frozenset({"no", "maybe", "yes"})


class TaggerError(PeopleArchiveError):
    """The tagger's answer could not be read."""


class _TagLine(FrozenModel):
    """One entry of the tagger's JSON answer."""

    n: int = Field(description="Entry number")
    interests: list[str] = Field(default_factory=list, description="Topic tags")
    background: list[str] = Field(default_factory=list, description="Role tags")
    reach: str = Field(default="none", description="none / some / high")
    reach_why: str = Field(default="", description="Why")
    fit: str = Field(default="maybe", description="no / maybe / yes")
    fit_why: str = Field(default="", description="Why")


def _meeting_titles_by_address(store: ArchiveStore) -> dict[str, list[str]]:
    """Latest few distinct meeting titles per attendee address, from one pass over the calendar."""
    titles: dict[str, list[str]] = {}
    for summary, attendees_json in store.execute_read(
        "SELECT summary, attendees_json FROM calendar_events WHERE summary != '' ORDER BY start_ts DESC", []
    ):
        for attendee in json.loads(attendees_json):
            bucket = titles.setdefault(str(attendee[0]), [])
            if len(bucket) < _MEETING_TITLES_PER_PERSON and str(summary) not in bucket:
                bucket.append(str(summary))
    return titles


def _evidence_for(store: ArchiveStore, row: tuple[Any, ...], titles_by_address: dict[str, list[str]]) -> str:
    person_id, name, emails_json, organization, title, category, x_username = row
    emails = json.loads(emails_json)
    lines = [f"name: {name}", f"organization: {organization or '-'} · title: {title or '-'} · the user's category: {category or '-'}"]
    if emails:
        lines.append("email domains: " + ", ".join(sorted({e.split('@', 1)[1] for e in emails})))
    if x_username:
        lines.append(f"X/Twitter: @{x_username}")
    if emails:
        placeholders = ",".join("?" for _ in emails)
        subjects = store.execute_read(
            "SELECT DISTINCT m.subject FROM message_participants p JOIN messages m ON m.account = p.account AND m.message_id = p.message_id "
            f"WHERE m.is_automated = 0 AND m.subject != '' AND p.address IN ({placeholders}) ORDER BY m.ts DESC LIMIT ?",
            [*emails, _SUBJECTS_PER_PERSON],
        )
        if subjects:
            lines.append("recent subjects: " + " | ".join(str(s[0])[:70] for s in subjects))
        titles: list[str] = []
        for email in emails:
            titles.extend(t for t in titles_by_address.get(email, []) if t not in titles)
        if titles:
            lines.append("meetings: " + " | ".join(t[:60] for t in titles[:_MEETING_TITLES_PER_PERSON]))
    notes = store.execute_read(
        "SELECT n.text FROM note_mentions nm JOIN notes n ON n.source = nm.source AND n.path = nm.path WHERE nm.handle_key = ? LIMIT ?",
        [f"note:{str(name).lower()}", _NOTE_LINES_PER_PERSON],
    )
    for (text,) in notes:
        needle = str(name).lower()
        hits = [line.strip() for line in str(text).splitlines() if needle in line.lower()]
        if hits:
            lines.append("the user's note: " + " ".join(hits[:2])[:240])
    return "\n".join(lines)


@pure
def _parse(text: str, expected: int) -> list[_TagLine]:
    stripped = text.strip()
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start < 0 or end < start:
        raise TaggerError(f"No JSON array: {stripped[:100]!r}")
    try:
        lines = [_TagLine.model_validate(item) for item in json.loads(stripped[start:end + 1])]
    except (json.JSONDecodeError, ValidationError) as e:
        raise TaggerError(f"Unreadable answer: {e}") from e
    if len(lines) != expected:
        raise TaggerError(f"Answered {len(lines)} of {expected}")
    return lines


def _tag_batch(person_ids: Sequence[str], evidence: Sequence[str], model: str) -> list[PersonTagsRow]:
    prompt = "Entries:\n\n" + "\n\n".join(f"{idx + 1}.\n{text}" for idx, text in enumerate(evidence))
    try:
        result = claude_p_completion(prompt, system=_SYSTEM_PROMPT + "\nUser context: " + load_settings().owner_context + "\nOutreach topic: " + load_settings().outreach_context, model=model)
    except ClaudeCLIError as e:
        raise TaggerError(f"Tagger call failed: {e}") from e
    tagged_at = datetime.now(timezone.utc)
    rows: list[PersonTagsRow] = []
    for line in _parse(result.text, len(person_ids)):
        if not 1 <= line.n <= len(person_ids):
            continue
        rows.append(PersonTagsRow(
            person_id=PersonId(person_ids[line.n - 1]),
            interests_json=json.dumps([t.strip().lower() for t in line.interests if t.strip()][:8]),
            background_json=json.dumps([t.strip().lower() for t in line.background if t.strip()][:4]),
            reach=line.reach.lower() if line.reach.lower() in _REACH_VALUES else "none", reach_why=line.reach_why[:200],
            fit=line.fit.lower() if line.fit.lower() in _FIT_VALUES else "maybe", fit_why=line.fit_why[:200],
            tagged_at=tagged_at, model=model,
        ))
    return rows


def tag_untagged_people(store: ArchiveStore, model: str, limit: int) -> int:
    """Tag close-or-known, non-colleague humans that have no tags yet; returns how many were tagged."""
    rows = store.execute_read(
        "SELECT p.person_id, p.canonical_name, p.emails_json, p.organization, p.title, p.hand_kept_category, "
        "COALESCE((SELECT b.username FROM person_handles h JOIN beeper_participants b ON b.handle_key = h.handle_key "
        "          WHERE h.person_id = p.person_id AND h.handle_key LIKE 'x:%' AND b.username != '' LIMIT 1), '') "
        "FROM persons p LEFT JOIN person_tags t ON t.person_id = p.person_id "
        "WHERE p.is_human = 1 AND p.tier <= 2 AND p.is_colleague = 0 AND t.person_id IS NULL ORDER BY p.score DESC LIMIT ?",
        [limit],
    )
    titles_by_address = _meeting_titles_by_address(store)
    evidence = [(str(row[0]), _evidence_for(store, row, titles_by_address)) for row in rows]
    batches = [evidence[i:i + PEOPLE_PER_CALL] for i in range(0, len(evidence), PEOPLE_PER_CALL)]
    tagged = 0

    def run(batch: Sequence[tuple[str, str]]) -> list[PersonTagsRow]:
        try:
            return _tag_batch([pid for pid, _ in batch], [text for _, text in batch], model)
        except TaggerError as e:
            logger.warning("Tagger batch skipped: {}", e)
            return []

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for result_rows in pool.map(run, batches):
            if result_rows:
                store.save_person_tags(result_rows)
                tagged += len(result_rows)
    logger.info("Tagged {} people in {} batches", tagged, len(batches))
    return tagged
