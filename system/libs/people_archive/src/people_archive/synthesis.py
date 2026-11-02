import json
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

from people_archive.claude_p import ClaudeCLIError, claude_p_completion
from people_archive.errors import PeopleArchiveError
from people_archive.primitives import PersonId
from people_archive.store import ArchiveStore, SynthesisRow

# Which model writes the dossier prose. Sonnet: the evidence packs are long and the
# summaries are read by a human; a one-line change to swap.
SYNTHESIS_MODEL: Final[str] = "claude-sonnet-5"
# Evidence budget per person: newest first for messages, longest first for meetings and notes.
_MAX_EMAILS: Final[int] = 40
_MAX_TEXT_DAYS: Final[int] = 30
_MAX_MEETINGS: Final[int] = 6
_MAX_NOTES: Final[int] = 25
_EMAIL_CHARS: Final[int] = 700
_TEXT_DAY_CHARS: Final[int] = 600
_MEETING_CHARS: Final[int] = 4000
_NOTE_CHARS: Final[int] = 900

_SYSTEM_PROMPT: Final[str] = """You write the private CRM dossier of one person for the user, who will SCAN it, not
read it. You get an evidence pack: facts, then dated excerpts from emails, texts, meeting transcripts,
and the user's own notes. Never invent; when the evidence is thin, say so in one bullet.

Rules for every section:
- Bullets only. One fact per bullet. At most 12 words before the citation. No paragraphs.
- Lead each bullet with the concrete noun or verb, not "He" / "the user" / "They".
- End each bullet with its evidence in parentheses: (2024-03 email), (2023-05 note), (2026-07 meeting), (2025-01 text).
  Use the excerpt's date; month precision is enough. the user's own notes outrank everything else.
- Plain words. No filler, no hedging words, no praise.

Answer with exactly these nine sections, each heading on its own line, bullets as "- " lines:

### ONE LINER
one sentence: who this person is to the user (no citation needed)
### KEY FACTS
3-6 bullets: role, organization, where they live, how you're connected, anything a stranger would need first
### HOW WE MET
1-3 bullets, or the single bullet "- Unknown from the evidence."
### CARES ABOUT
4-8 bullets: the work, ideas, and causes they keep returning to
### STATE
3-5 bullets: where the relationship is now and every open thread
### WATCH OUT
0-3 bullets: friction, sensitivities, patterns to be careful of; or the single bullet "- Nothing flagged."
### NEXT
1-3 bullets: concrete things the user might do next; or "- Nothing suggests itself."
### INTERESTS
2-8 short lowercase topic tags, comma-separated on one line
### BACKGROUND
1-4 role tags, comma-separated on one line, from: engineer, designer, founder, investor, researcher, operator, writer, artist, scientist, executive, other"""


class SynthesisError(PeopleArchiveError):
    """The model's answer could not be read as a dossier."""


class Dossier(FrozenModel):
    """The model-written sections of one person's page."""

    one_liner: str = Field(description="Who they are to the user")
    key_facts: str = Field(description="Bullets a stranger needs first")
    how_we_met: str = Field(description="How the relationship started, cited")
    what_they_care_about: str = Field(description="Interests and work, cited")
    where_things_stand: str = Field(description="Current state, cited")
    watch_out: str = Field(description="Friction and sensitivities, cited")
    interests: str = Field(description="JSON array string of topic tags")
    background: str = Field(description="JSON array string of role tags")
    next: str = Field(description="Suggested next moves")


def _evidence_pack(store: ArchiveStore, person_id: str) -> tuple[str, int]:
    """The facts and dated excerpts for one person; returns the pack and how many excerpts it holds."""
    person = store.execute_read(
        "SELECT canonical_name, aliases_json, emails_json, phones_json, organization, title, tier, hand_kept_category, "
        "sent_count, received_human_count, meeting_count, text_days_sent, text_days_received, first_contact_ts, last_contact_ts "
        "FROM persons WHERE person_id = ?", [person_id],
    )[0]
    name = str(person[0])
    emails = json.loads(person[2])
    lines = [
        f"PERSON: {name}", f"aliases: {', '.join(json.loads(person[1])) or '-'}", f"emails: {', '.join(emails) or '-'}",
        f"phones: {', '.join(json.loads(person[3])) or '-'}", f"organization: {person[4] or '-'} · title: {person[5] or '-'}",
        f"tier {person[6]} · contacts-file category: {person[7] or '-'}",
        f"counts: {person[8]} emails the user sent, {person[9]} received, {person[10]} meeting series, {person[11]} days the user texted, {person[12]} days they texted",
        f"first contact {_date(person[13])} · last contact {_date(person[14])}", "",
    ]
    excerpts = 0
    # Emails, newest first, non-bulk.
    if emails:
        placeholders = ",".join("?" for _ in emails)
        rows = store.execute_read(
            "SELECT DISTINCT m.ts, m.direction, m.subject, m.text FROM message_participants p JOIN messages m "
            "ON m.account = p.account AND m.message_id = p.message_id "
            f"WHERE m.is_automated = 0 AND p.address IN ({placeholders}) ORDER BY m.ts DESC LIMIT ?",
            [*emails, _MAX_EMAILS],
        )
        if rows:
            lines.append("== EMAILS (newest first) ==")
        for ts, direction, subject, text in rows:
            who = "the user wrote" if direction == "SENT" else f"{name} wrote"
            lines.append(f"[{_date(ts)} email] {who}: {subject} — {_squash(text, _EMAIL_CHARS)}")
            excerpts += 1
        lines.append("")
    # Texts: one excerpt per day, newest days first.
    text_rows = store.execute_read(
        "SELECT m.ts / 86400 AS day, c.network, c.title, GROUP_CONCAT((CASE WHEN m.is_sender THEN 'the user' ELSE m.sender_name END) || ': ' || m.text, ' | ') "
        "FROM person_handles h JOIN beeper_participants p ON p.handle_key = h.handle_key AND p.is_self = 0 "
        "JOIN beeper_chats c ON c.chat_id = p.chat_id JOIN beeper_messages m ON m.chat_id = c.chat_id "
        "WHERE h.person_id = ? AND m.text != '' AND c.chat_type = 'single' GROUP BY day, c.chat_id ORDER BY day DESC LIMIT ?",
        [person_id, _MAX_TEXT_DAYS],
    )
    if text_rows:
        lines.append("== TEXTS (one line per day, newest first) ==")
    for day, network, title, joined in text_rows:
        lines.append(f"[{_date(int(day) * 86400)} text/{network}] {_squash(joined, _TEXT_DAY_CHARS)}")
        excerpts += 1
    lines.append("")
    # Recorded meetings the person attended (via the matched calendar event), longest first.
    if emails:
        like_clauses = " OR ".join("attendee_emails_json LIKE ?" for _ in emails)
        meetings = store.execute_read(
            f"SELECT started_ts, title, summary, transcript FROM meetings WHERE {like_clauses} ORDER BY LENGTH(transcript) DESC LIMIT ?",
            [*[f'%"{e}"%' for e in emails], _MAX_MEETINGS],
        )
        if meetings:
            lines.append("== RECORDED MEETINGS ==")
        for ts, title, summary, transcript in meetings:
            body = summary if summary.strip() else transcript
            lines.append(f"[{_date(ts)} meeting] {title}: {_squash(body, _MEETING_CHARS)}")
            excerpts += 1
        lines.append("")
    # the user's notes that link the person's page name(s).
    note_keys = [f"note:{n.lower()}" for n in [name, *json.loads(person[1])]]
    placeholders = ",".join("?" for _ in note_keys)
    notes = store.execute_read(
        "SELECT n.note_date_ts, n.source, n.title, n.text FROM note_mentions nm JOIN notes n ON n.source = nm.source AND n.path = nm.path "
        f"WHERE nm.handle_key IN ({placeholders}) ORDER BY COALESCE(n.note_date_ts, 0) DESC LIMIT ?",
        [*note_keys, _MAX_NOTES],
    )
    if notes:
        lines.append("== USER'S OWN NOTES (highest authority) ==")
    for ts, source, title, text in notes:
        lines.append(f"[{_date(ts) if ts else 'undated'} note/{source}] {title}: {_squash(_lines_mentioning(text, name), _NOTE_CHARS)}")
        excerpts += 1
    return "\n".join(lines), excerpts


@pure
def _date(ts: Any) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d") if ts else "undated"


@pure
def _squash(text: str, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


@pure
def _lines_mentioning(text: str, name: str) -> str:
    """The lines of a note that mention the name (plus one line after), or the head of the note."""
    needle = name.lower()
    lines = text.splitlines()
    picked: list[str] = []
    for idx, line in enumerate(lines):
        if needle in line.lower() or f"[[{needle}" in line.lower():
            picked.append(line.strip())
            if idx + 1 < len(lines):
                picked.append(lines[idx + 1].strip())
    return "\n".join(picked) if picked else text


_SECTION_HEADINGS: Final[dict[str, str]] = {
    "ONE LINER": "one_liner", "KEY FACTS": "key_facts", "HOW WE MET": "how_we_met", "CARES ABOUT": "what_they_care_about",
    "STATE": "where_things_stand", "WATCH OUT": "watch_out", "NEXT": "next", "INTERESTS": "interests", "BACKGROUND": "background",
}


@pure
def _bullet_lines(lines: Sequence[str]) -> str:
    """Normalize a section to one '- ' bullet per line."""
    cleaned = [line.strip().lstrip("-*•").strip() for line in lines if line.strip()]
    return "\n".join(f"- {line}" for line in cleaned if line)


@pure
def _parse_dossier(text: str) -> Dossier:
    """Split the answer on its seven headings; comma lists become JSON arrays. Raises SynthesisError if a section is missing."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        heading = line.strip().lstrip("#").strip().upper()
        if heading in _SECTION_HEADINGS:
            current = _SECTION_HEADINGS[heading]
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    missing = [key for key in _SECTION_HEADINGS.values() if key not in sections]
    if missing:
        raise SynthesisError(f"Answer lacks sections {missing}: {text[:120]!r}")
    # Bullet sections keep one bullet per line; the one-liner and tag lines collapse to one line.
    bullets = {key: _bullet_lines(lines) for key, lines in sections.items()}
    joined = {key: " ".join(" ".join(lines).split()) for key, lines in sections.items()}
    return Dossier(
        one_liner=joined["one_liner"], key_facts=bullets["key_facts"], how_we_met=bullets["how_we_met"],
        what_they_care_about=bullets["what_they_care_about"], where_things_stand=bullets["where_things_stand"],
        watch_out=("" if "nothing flagged" in joined["watch_out"].lower() else bullets["watch_out"]),
        next=("" if "nothing suggests itself" in joined["next"].lower() else bullets["next"]),
        interests=json.dumps([tag.strip().strip(".").lower() for tag in joined["interests"].split(",") if tag.strip()]),
        background=json.dumps([tag.strip().strip(".").lower() for tag in joined["background"].split(",") if tag.strip()]),
    )


def synthesize_person(store: ArchiveStore, person_id: str, model: str) -> Dossier:
    pack, excerpt_count = _evidence_pack(store, person_id)
    try:
        result = claude_p_completion("Evidence pack:\n\n" + pack, system=_SYSTEM_PROMPT, model=model)
    except ClaudeCLIError as e:
        raise SynthesisError(f"Model call failed: {e}") from e
    dossier = _parse_dossier(result.text)
    store.save_synthesis(SynthesisRow(
        person_id=PersonId(person_id), model=model, synthesized_at=datetime.now(timezone.utc), excerpt_count=excerpt_count,
        one_liner=dossier.one_liner, key_facts=dossier.key_facts, how_we_met=dossier.how_we_met, what_they_care_about=dossier.what_they_care_about,
        where_things_stand=dossier.where_things_stand, watch_out=dossier.watch_out, interests_json=dossier.interests, background_json=dossier.background, next_steps=dossier.next,
        cost_usd=float(result.cost_usd),
    ))
    logger.info("Synthesized {} from {} excerpts (${:.3f})", person_id, excerpt_count, result.cost_usd)
    return dossier


def synthesize_people_by_name(store: ArchiveStore, names: Sequence[str], model: str) -> list[tuple[str, Dossier]]:
    written: list[tuple[str, Dossier]] = []
    for name in names:
        rows = store.execute_read("SELECT person_id FROM persons WHERE canonical_name = ? AND is_human = 1 ORDER BY score DESC LIMIT 1", [name])
        if not rows:
            logger.warning("No person named {}", name)
            continue
        written.append((str(rows[0][0]), synthesize_person(store, str(rows[0][0]), model)))
    return written
