"""Judge the "possibly the same person" pairs from the archive's own evidence.

The identity pass proposes two people who share a full name but have nothing hard linking them.
This module gathers what the archive knows about each side (signatures, organizations, contact
cards, shared emails and meetings, address switches inside one thread), asks a model, and records
the confident calls in the machine's own ledger so the next identity pass applies them. Everything
else stays on the review page with the model's lean and reason next to it.
"""

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field, ValidationError

from people_archive.assertions import Decision, auto_decisions_path, make_assertion
from people_archive.claude_p import ClaudeCLIError, claude_p_completion
from people_archive.errors import PeopleArchiveError
from people_archive.store import ArchiveStore, MergeJudgmentRow

MERGE_JUDGE_MODEL: Final[str] = "claude-sonnet-5"
PAIRS_PER_CALL: Final[int] = 5
# Confident machine decisions remain separate from human decisions.
AUTO_SAME_CONFIDENCE: Final[float] = 0.7
AUTO_DIFFERENT_CONFIDENCE: Final[float] = 0.8
# Each call is a separate claude process (~300 MB); more than a few at once gets them killed for memory.
_WORKERS: Final[int] = 2
_MESSAGES_PER_SIDE: Final[int] = 3
_SIGNATURE_LINES: Final[int] = 6
_LINE_CHARS: Final[int] = 90
_EVENTS_PER_SIDE: Final[int] = 3
_EXAMPLES_PER_SIGNAL: Final[int] = 2

_SYSTEM_PROMPT: Final[str] = """You decide whether two entries in the user's personal CRM are the same human being.
Each numbered pair shows two entries that share a full name but have no hard link (no shared contact card,
no shared address). For each side you get its addresses, organization, dates, message counts, contact-card
lines, recent messages it sent (subject plus the signature lines at the foot), meetings, and then the
signals that involve both sides together.

Weigh the evidence like a careful assistant would:
- Strong signs of SAME: a signature under one address names the other side's company, title, phone, or
  address; one side cc's or forwards to the other side's address on its own mail ("cc'ing my personal");
  a reply inside one thread comes from the other side's address with the same voice or signature; the
  same job title or organization on both sides; an address switch that fits a job change (old company
  goes quiet as the new one starts); a distinctive name with matching context on both sides.
- Strong signs of DIFFERENT: both sides are invited separately to the same meeting or are separate
  recipients of the same email with no sign of self-cc; different organizations, titles, or cities
  active at the same time; different first-name spellings or middle names; a common name (e.g. David Kim,
  Sarah Lee, Michael Chen) with nothing linking the two contexts.
- With a common name and no linking evidence, say DIFFERENT or UNSURE, never SAME on the name alone.
- Each pair says how many people in this archive carry the name. A name carried by only these
  two is rare here; a name carried by four or more is common.
- A side that is only a phone number or chat handle (texts, Telegram, Signal, X, Facebook) with no email:
  Do not assume the user emails everyone they text. If the name is rare in the archive
  and the texting side's chats fit the email side's world (same colleagues, company, city, events, topics,
  or a group chat named for that circle) with nothing contradicting, say SAME at 0.7-0.85. If the name is
  common or the texting side's context points elsewhere, UNSURE or DIFFERENT.
- Otherwise an entry with almost no evidence next to a rich one is UNSURE unless a signal links them.

Return ONLY a JSON array, one object per pair, same order:
{"n": <number>, "verdict": "same" | "different" | "unsure", "confidence": <0.0-1.0>, "reason": "<one or two short clauses citing the decisive evidence>"}"""

_VERDICTS: Final[frozenset[str]] = frozenset({"same", "different", "unsure"})
_QUOTED_LINE = re.compile(r"^\s*(>|On .* wrote:|From: |Sent: |To: )")


class MergeJudgeError(PeopleArchiveError):
    """The judge's answer could not be read."""


class _JudgmentLine(FrozenModel):
    """One entry of the judge's JSON answer."""

    n: int = Field(description="Pair number")
    verdict: str = Field(description="same / different / unsure")
    confidence: float = Field(default=0.0, description="0..1")
    reason: str = Field(default="", description="Why")


class _Side(FrozenModel):
    """One person of a pair, as the archive knows them."""

    person_id: str = Field(description="Stable id")
    name: str = Field(description="Canonical name")
    handles: tuple[str, ...] = Field(description="Every handle key")
    emails: tuple[str, ...] = Field(description="Email addresses")
    evidence: str = Field(description="Evidence lines for the prompt")


class _Pair(FrozenModel):
    """A candidate pair with everything the prompt needs."""

    side_a: _Side = Field(description="First person")
    side_b: _Side = Field(description="Second person")
    joint: str = Field(description="Signals involving both sides")
    name_count: int = Field(description="How many people in the archive carry this name")

    @property
    def pair_key(self) -> str:
        return pair_key_for(self.side_a.handles, self.side_b.handles)


@pure
def pair_key_for(handles_a: Sequence[str], handles_b: Sequence[str]) -> str:
    """Order-free key of a pair: both sides sorted, then the sides sorted."""
    sides = sorted(("|".join(sorted(handles_a)), "|".join(sorted(handles_b))))
    return "||".join(sides)


@pure
def _day(ts: Any) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d") if ts is not None else "?"


@pure
def _signature_tail(text: str) -> str:
    lines = [line.strip()[:_LINE_CHARS] for line in text.strip().splitlines() if line.strip()]
    kept: list[str] = []
    for line in lines:
        if _QUOTED_LINE.match(line):
            break
        kept.append(line)
    return " / ".join(kept[-_SIGNATURE_LINES:])


# (start_ts, summary, event key) per attendee address, newest first; built once per run instead of scanning
# every event's attendee list once per pair.
_Event = tuple[int, str, str]


def _events_by_address(store: ArchiveStore) -> dict[str, list[_Event]]:
    found: dict[str, list[_Event]] = defaultdict(list)
    for account, event_id, summary, start, attendees_json in store.execute_read(
        "SELECT account, event_id, summary, start_ts, attendees_json FROM calendar_events ORDER BY start_ts DESC", [],
    ):
        for attendee in json.loads(str(attendees_json)):
            if attendee and attendee[0]:
                found[str(attendee[0]).lower()].append((int(start), str(summary), f"{account}/{event_id}"))
    return found


@pure
def _events_of(events_by_address: Mapping[str, list[_Event]], emails: Sequence[str]) -> list[_Event]:
    seen: set[str] = set()
    events: list[_Event] = []
    for email in emails:
        for event in events_by_address.get(email.lower(), ()):
            if event[2] not in seen:
                seen.add(event[2])
                events.append(event)
    return sorted(events, key=lambda event: -event[0])


def _side_for(store: ArchiveStore, person_id: str, events_by_address: Mapping[str, list[_Event]]) -> _Side | None:
    rows = store.execute_read(
        "SELECT canonical_name, emails_json, phones_json, organization, title, first_contact_ts, last_contact_ts, "
        "sent_count, received_human_count, meeting_count, text_days_sent + text_days_received, aliases_json FROM persons WHERE person_id = ?",
        [person_id],
    )
    if not rows:
        return None
    row = rows[0]
    emails = tuple(str(e) for e in json.loads(row[1]))
    phones = tuple(str(p) for p in json.loads(row[2]))
    handles = tuple(str(r[0]) for r in store.execute_read("SELECT handle_key FROM person_handles WHERE person_id = ?", [person_id]))
    lines = [
        f"addresses: {', '.join(emails) or '-'}; phones: {', '.join(phones) or '-'}; other handles: {', '.join(h for h in handles if not h.startswith(('email:', 'phone:'))) or '-'}",
        f"organization: {row[3] or '-'}; title: {row[4] or '-'}; also seen as: {', '.join(str(a) for a in json.loads(row[11])) or '-'}",
        f"active {_day(row[5])} to {_day(row[6])}; the user sent {row[7]}, received {row[8]}, meetings {row[9]}, text days {row[10]}",
    ]
    if emails:
        marks = " OR ".join("emails_json LIKE ?" for _ in emails)
        for card in store.execute_read(f"SELECT display_name, organization, title FROM contacts WHERE {marks} LIMIT 3", [f'%"{e}"%' for e in emails]):
            lines.append(f"contact card: {card[0]} · {card[1] or '-'} · {card[2] or '-'}")
        placeholders = ", ".join("?" * len(emails))
        # Go through message_participants (indexed by address); messages has no index on the sender.
        for ts, subject, text in store.execute_read(
            "SELECT m.ts, m.subject, m.text FROM message_participants p JOIN messages m ON m.account = p.account AND m.message_id = p.message_id "
            f"WHERE p.role = 'from' AND p.address IN ({placeholders}) AND m.is_automated = 0 ORDER BY p.ts DESC LIMIT {_MESSAGES_PER_SIDE}", list(emails),
        ):
            lines.append(f"sent {_day(ts)} \"{str(subject)[:80]}\" ends: {_signature_tail(str(text))}")
        for ts, subject in store.execute_read(
            f"SELECT m.ts, m.subject FROM message_participants p JOIN messages m ON m.account = p.account AND m.message_id = p.message_id "
            f"WHERE p.address IN ({placeholders}) AND m.direction = 'SENT' ORDER BY m.ts DESC LIMIT 2", list(emails),
        ):
            lines.append(f"the user wrote {_day(ts)} \"{str(subject)[:80]}\"")
        for start, summary, _event in _events_of(events_by_address, emails)[:_EVENTS_PER_SIDE]:
            lines.append(f"meeting {_day(start)} \"{summary[:80]}\"")
    lines.extend(_chat_evidence(store, [h for h in handles if not h.startswith("email:")]))
    return _Side(person_id=person_id, name=str(row[0]), handles=handles, emails=emails, evidence="\n".join(lines))


_CHATS_PER_SIDE: Final[int] = 4
_TEXTS_PER_SIDE: Final[int] = 4


def _chat_evidence(store: ArchiveStore, handles: Sequence[str]) -> list[str]:
    """What the texting side of a person looks like: their chats, who else is in them, and a few of their texts."""
    if not handles:
        return []
    placeholders = ", ".join("?" * len(handles))
    memberships = store.execute_read(
        "SELECT p.chat_id, p.participant_id, p.full_name, p.username, c.network, c.title, c.chat_type FROM beeper_participants p "
        f"JOIN beeper_chats c ON c.chat_id = p.chat_id WHERE p.handle_key IN ({placeholders}) ORDER BY c.last_activity_ts DESC LIMIT {_CHATS_PER_SIDE}",
        list(handles),
    )
    lines: list[str] = []
    texts: list[str] = []
    for chat_id, participant_id, full_name, username, network, title, chat_type in memberships:
        others = [str(r[0]) for r in store.execute_read(
            "SELECT full_name FROM beeper_participants WHERE chat_id = ? AND participant_id != ? AND is_self = 0 AND full_name != '' LIMIT 4", [chat_id, participant_id],
        )]
        shown = f"{full_name or '-'}" + (f" (@{username})" if username else "")
        lines.append(f"{network} {chat_type} chat \"{str(title)[:50]}\" as {shown}" + (f" with {', '.join(others)}" if others else ""))
        if len(texts) < _TEXTS_PER_SIDE:
            for ts, text in store.execute_read(
                "SELECT ts, text FROM beeper_messages WHERE chat_id = ? AND sender_id = ? AND text != '' ORDER BY ts DESC LIMIT ?",
                [chat_id, participant_id, _TEXTS_PER_SIDE - len(texts)],
            ):
                texts.append(f"texted {_day(ts)}: {str(text)[:_LINE_CHARS]}")
    return lines + texts


def _joint_signals(store: ArchiveStore, side_a: _Side, side_b: _Side, events_by_address: Mapping[str, list[_Event]]) -> str:
    if not side_a.emails or not side_b.emails:
        return "no shared-record signals (one side has no email)"
    marks_a = ", ".join("?" * len(side_a.emails))
    marks_b = ", ".join("?" * len(side_b.emails))
    lines: list[str] = []
    co_messages = store.execute_read(
        "SELECT m.ts, m.subject, pa.role, pb.role, m.from_addr FROM message_participants pa "
        "JOIN message_participants pb ON pb.account = pa.account AND pb.message_id = pa.message_id "
        "JOIN messages m ON m.account = pa.account AND m.message_id = pa.message_id "
        f"WHERE pa.address IN ({marks_a}) AND pb.address IN ({marks_b}) AND m.is_automated = 0 ORDER BY m.ts DESC LIMIT 50",
        [*side_a.emails, *side_b.emails],
    )
    if co_messages:
        examples = "; ".join(f"{_day(r[0])} \"{str(r[1])[:60]}\" (A as {r[2]}, B as {r[3]}, from {r[4]})" for r in co_messages[:_EXAMPLES_PER_SIGNAL])
        lines.append(f"both on the same email {len(co_messages)} time(s): {examples}")
    # Threads each side wrote in, intersected here: a joined query on thread ids plans badly.
    threads_b = {(str(r[0]), str(r[1])): int(r[2]) for r in _threads_written_in(store, side_b.emails)}
    shared_threads = [
        (str(subject), int(ts), threads_b[(str(account), str(thread))])
        for account, thread, ts, subject in _threads_written_in(store, side_a.emails) if (str(account), str(thread)) in threads_b
    ]
    if shared_threads:
        examples = "; ".join(f"\"{subject[:60]}\" (A wrote {_day(ts_a)}, B wrote {_day(ts_b)})" for subject, ts_a, ts_b in shared_threads[:_EXAMPLES_PER_SIGNAL])
        lines.append(f"messages from both addresses inside the same thread, {len(shared_threads)} time(s): {examples}")
    keys_b = {event[2] for event in _events_of(events_by_address, side_b.emails)}
    co_events = [event for event in _events_of(events_by_address, side_a.emails) if event[2] in keys_b]
    if co_events:
        examples = "; ".join(f"{_day(start)} \"{summary[:60]}\"" for start, summary, _key in co_events[:_EXAMPLES_PER_SIGNAL])
        lines.append(f"both invited to the same meeting {len(co_events)} time(s): {examples}")
    return "\n".join(lines) or "no shared-record signals (never on the same email, thread, or meeting)"


_THREADS_PER_SIDE: Final[int] = 400


def _threads_written_in(store: ArchiveStore, emails: Sequence[str]) -> list[tuple[Any, ...]]:
    """(account, thread_id, ts, subject) of the newest human messages these addresses sent."""
    placeholders = ", ".join("?" * len(emails))
    return store.execute_read(
        "SELECT m.account, m.thread_id, m.ts, m.subject FROM message_participants p JOIN messages m ON m.account = p.account AND m.message_id = p.message_id "
        f"WHERE p.role = 'from' AND p.address IN ({placeholders}) AND m.is_automated = 0 ORDER BY p.ts DESC LIMIT {_THREADS_PER_SIDE}",
        list(emails),
    )


def _pairs_to_judge(store: ArchiveStore, limit: int, rejudge: bool) -> list[_Pair]:
    judged = set() if rejudge else {j.pair_key for j in store.merge_judgments()}
    events_by_address = _events_by_address(store)
    name_counts = {str(r[0]): int(r[1]) for r in store.execute_read("SELECT lower(canonical_name), count(*) FROM persons WHERE is_human = 1 GROUP BY 1", [])}
    pairs: list[_Pair] = []
    for person_a, person_b in store.execute_read("SELECT person_id_a, person_id_b FROM merge_candidates", []):
        side_a = _side_for(store, str(person_a), events_by_address)
        side_b = _side_for(store, str(person_b), events_by_address)
        if side_a is None or side_b is None or not side_a.handles or not side_b.handles:
            continue
        if pair_key_for(side_a.handles, side_b.handles) in judged:
            continue
        pairs.append(_Pair(
            side_a=side_a, side_b=side_b, joint=_joint_signals(store, side_a, side_b, events_by_address),
            name_count=name_counts.get(side_a.name.lower(), 2),
        ))
        if len(pairs) >= limit:
            break
    return pairs


@pure
def _prompt_for(pairs: Sequence[_Pair]) -> str:
    blocks = []
    for idx, pair in enumerate(pairs):
        blocks.append(
            f"{idx + 1}. Name: {pair.side_a.name} ({pair.name_count} people in the archive carry this name)\n"
            f"Side A:\n{pair.side_a.evidence}\nSide B:\n{pair.side_b.evidence}\nTogether:\n{pair.joint}"
        )
    return "Pairs:\n\n" + "\n\n".join(blocks)


@pure
def _parse(text: str, expected: int) -> list[_JudgmentLine]:
    stripped = text.strip()
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start < 0 or end < start:
        # A one-pair batch sometimes comes back as a bare object.
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end < start:
            raise MergeJudgeError(f"No JSON array: {stripped[:100]!r}")
        stripped = f"[{stripped[start:end + 1]}]"
        start, end = 0, len(stripped) - 1
    try:
        lines = [_JudgmentLine.model_validate(item) for item in json.loads(stripped[start:end + 1])]
    except (json.JSONDecodeError, ValidationError) as e:
        raise MergeJudgeError(f"Unreadable answer: {e}") from e
    if len(lines) != expected:
        raise MergeJudgeError(f"Answered {len(lines)} of {expected}")
    return lines


def judge_batch(pairs: Sequence[_Pair], model: str) -> list[MergeJudgmentRow]:
    try:
        result = claude_p_completion(_prompt_for(pairs), system=_SYSTEM_PROMPT, model=model)
    except ClaudeCLIError as e:
        raise MergeJudgeError(f"Judge call failed: {e}") from e
    judged_at = datetime.now(timezone.utc)
    rows: list[MergeJudgmentRow] = []
    for line in _parse(result.text, len(pairs)):
        if not 1 <= line.n <= len(pairs):
            continue
        pair = pairs[line.n - 1]
        verdict = line.verdict.strip().lower()
        rows.append(MergeJudgmentRow(
            pair_key=pair.pair_key, handles_a=pair.side_a.handles, handles_b=pair.side_b.handles, name=pair.side_a.name,
            verdict=verdict if verdict in _VERDICTS else "unsure", confidence=min(1.0, max(0.0, line.confidence)),
            reason=line.reason.strip()[:300], judged_at=judged_at, model=model,
        ))
    return rows


@pure
def decision_for(row: MergeJudgmentRow) -> Decision | None:
    """The ledger entry a judgment earns, if it is confident enough to act on without the user."""
    if row.verdict == "same" and row.confidence >= AUTO_SAME_CONFIDENCE:
        return Decision.SAME
    if row.verdict == "different" and row.confidence >= AUTO_DIFFERENT_CONFIDENCE:
        return Decision.DIFFERENT
    return None


class MergeJudgeSummary(FrozenModel):
    """What one run did."""

    judged: int = Field(description="Pairs judged now")
    same: int = Field(description="Recorded as the same person")
    different: int = Field(description="Recorded as different people")
    review: int = Field(description="Left for the user")


def judge_merge_candidates(store: ArchiveStore, model: str, limit: int, rejudge: bool = False) -> MergeJudgeSummary:
    """Judge unjudged candidate pairs; confident calls go to the machine ledger for the next identity pass."""
    pairs = _pairs_to_judge(store, limit, rejudge)
    batches = [pairs[i:i + PAIRS_PER_CALL] for i in range(0, len(pairs), PAIRS_PER_CALL)]
    counts = {"judged": 0, "same": 0, "different": 0, "review": 0}

    def run(batch: Sequence[_Pair]) -> list[MergeJudgmentRow]:
        try:
            return judge_batch(batch, model)
        except MergeJudgeError as e:
            logger.warning("Merge judge batch skipped: {}", e)
            return []

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for rows in pool.map(run, batches):
            if not rows:
                continue
            store.save_merge_judgments(rows)
            for row in rows:
                counts["judged"] += 1
                decision = decision_for(row)
                counts["review" if decision is None else decision.value.lower()] += 1
    write_auto_decisions(store)
    logger.info("Judged {} pairs in {} batches: {} same, {} different, {} for review", counts["judged"], len(batches), counts["same"], counts["different"], counts["review"])
    return MergeJudgeSummary(**counts)


def write_auto_decisions(store: ArchiveStore) -> int:
    """Rewrite the machine ledger from every stored judgment at the current thresholds; returns how many it holds.

    The judgments table is the truth; the ledger is the identity pass's view of it, so a threshold change
    takes effect at the next rebuild without judging anything again.
    """
    path = auto_decisions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w") as handle:
        for row in sorted(store.merge_judgments(), key=lambda j: j.judged_at):
            decision = decision_for(row)
            if decision is None:
                continue
            note = f"merge judge ({row.model}, {row.confidence:.2f}): {row.reason}"
            handle.write(make_assertion(decision, row.handles_a, row.handles_b, note=note).model_dump_json() + "\n")
            written += 1
    return written
