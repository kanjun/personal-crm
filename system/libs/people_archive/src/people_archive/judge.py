import json
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from enum import auto
from typing import Any, Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field, ValidationError

from people_archive.claude_p import ClaudeCLIError, claude_p_completion
from people_archive.errors import PeopleArchiveError
from people_archive.primitives import PersonId
from people_archive.store import ArchiveStore, JudgmentRow

# Which model judges. One-line change; independent of the chat's own model.
JUDGE_MODEL: Final[str] = "claude-haiku-4-5"
# People per prompt: enough to amortize the call, few enough to stay accurate.
PEOPLE_PER_CALL: Final[int] = 15
SAMPLE_MESSAGES_PER_PERSON: Final[int] = 4
_SNIPPET_CHARS: Final[int] = 140
_JUDGE_WORKERS: Final[int] = 3

_SYSTEM_PROMPT: Final[str] = """You judge whether entries in a personal CRM are real individual human beings.
You will get a numbered list of entries. Each has a display name, email addresses, an organization guess,
message counts, and sample subjects and snippets of messages associated with the address.
For each entry answer with exactly one verdict:
- PERSON: an individual human being (even if you only have a first name or the mail is terse).
- GROUP: a shared mailbox, mailing list, Google Group, team alias, department, or company-wide address.
- AUTOMATED: a system, bot, notification sender, newsletter, receipts, calendar/service mail, or a role
  address like press+subscribe@ or support@ that is not one specific human.
- UNSURE: genuinely cannot tell.
Return ONLY a JSON array, one object per entry, in the same order: {"n": <number>, "verdict": "...", "reason": "<one short clause>"}.
No prose before or after the JSON."""


class JudgeVerdict(UpperCaseStrEnum):
    """The judge's call on one entry."""

    PERSON = auto()
    GROUP = auto()
    AUTOMATED = auto()
    UNSURE = auto()


class JudgeError(PeopleArchiveError):
    """The judge returned something that could not be read as verdicts."""


class PersonJudgment(FrozenModel):
    """One stored verdict."""

    person_id: PersonId = Field(description="Whose")
    verdict: JudgeVerdict = Field(description="The call")
    reason: str = Field(description="The judge's one-clause reason")
    judged_at: datetime = Field(description="When")
    model: str = Field(description="Which model judged")


class _JudgeLine(FrozenModel):
    """One line of the judge's JSON answer."""

    n: int = Field(description="Entry number")
    verdict: JudgeVerdict = Field(description="The call")
    reason: str = Field(default="", description="Why")


def _evidence_for(store: ArchiveStore, person_row: tuple[Any, ...]) -> str:
    person_id, name, emails_json, organization, sent, received_human, received_automated, meetings = person_row
    emails = json.loads(emails_json)
    if emails:
        placeholders = ",".join("?" for _ in emails)
        samples = store.execute_read(
            "SELECT DISTINCT m.direction, m.subject, m.snippet FROM message_participants p "
            "JOIN messages m ON m.account = p.account AND m.message_id = p.message_id "
            f"WHERE p.address IN ({placeholders}) ORDER BY m.ts DESC LIMIT ?",
            [*emails, SAMPLE_MESSAGES_PER_PERSON],
        )
    else:
        samples = []
    lines = [
        f"name: {name}", f"emails: {', '.join(emails) or '(none)'}", f"organization guess: {organization or '(none)'}",
        f"counts: {sent} sent to them, {received_human} non-bulk from them, {received_automated} bulk from them, {meetings} meeting series",
    ]
    for direction, subject, snippet in samples:
        lines.append(f"  [{str(direction).lower()}] {str(subject)[:80]} — {str(snippet)[:_SNIPPET_CHARS]}")
    return "\n".join(lines)


@pure
def _parse_verdicts(text: str, expected_count: int) -> list[_JudgeLine]:
    """Raises JudgeError when the answer is not a JSON array of the expected length."""
    stripped = text.strip()
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start < 0 or end < start:
        raise JudgeError(f"No JSON array in judge answer: {stripped[:120]!r}")
    try:
        parsed = json.loads(stripped[start:end + 1])
        lines = [_JudgeLine.model_validate(item) for item in parsed]
    except (json.JSONDecodeError, ValidationError) as e:
        raise JudgeError(f"Unreadable judge answer: {e}") from e
    if len(lines) != expected_count:
        raise JudgeError(f"Judge answered {len(lines)} of {expected_count} entries")
    return lines


def _judge_batch(person_ids: Sequence[str], evidence: Sequence[str], model: str) -> list[PersonJudgment]:
    prompt_parts = [f"{idx + 1}.\n{text}" for idx, text in enumerate(evidence)]
    prompt = "Entries:\n\n" + "\n\n".join(prompt_parts)
    try:
        result = claude_p_completion(prompt, system=_SYSTEM_PROMPT, model=model)
    except ClaudeCLIError as e:
        raise JudgeError(f"Judge call failed: {e}") from e
    lines = _parse_verdicts(result.text, len(person_ids))
    judged_at = datetime.now(timezone.utc)
    return [
        PersonJudgment(person_id=PersonId(person_ids[line.n - 1]), verdict=line.verdict, reason=line.reason[:200], judged_at=judged_at, model=model)
        for line in sorted(lines, key=lambda line: line.n)
        if 1 <= line.n <= len(person_ids)
    ]


def judge_unjudged_people(store: ArchiveStore, model: str, limit: int) -> int:
    """Judge every human-flagged person without a stored verdict, cheapest evidence first; returns how many were judged."""
    rows = store.execute_read(
        "SELECT p.person_id, p.canonical_name, p.emails_json, p.organization, p.sent_count, p.received_human_count, "
        "p.received_automated_count, p.meeting_count FROM persons p LEFT JOIN person_judgments j ON j.person_id = p.person_id "
        "WHERE p.is_human = 1 AND j.person_id IS NULL ORDER BY p.score DESC LIMIT ?",
        [limit],
    )
    evidence_by_id = [(str(row[0]), _evidence_for(store, row)) for row in rows]
    batches = [evidence_by_id[start:start + PEOPLE_PER_CALL] for start in range(0, len(evidence_by_id), PEOPLE_PER_CALL)]
    judged = 0
    failed_batches = 0

    def run(batch: Sequence[tuple[str, str]]) -> list[PersonJudgment]:
        try:
            return _judge_batch([pid for pid, _ in batch], [text for _, text in batch], model)
        except JudgeError as e:
            logger.warning("Judge batch skipped: {}", e)
            return []

    with ThreadPoolExecutor(max_workers=_JUDGE_WORKERS) as pool:
        for verdicts in pool.map(run, batches):
            if not verdicts:
                failed_batches += 1
                continue
            rows = [
                JudgmentRow(person_id=v.person_id, verdict=v.verdict.value, reason=v.reason, judged_at=v.judged_at, model=v.model)
                for v in verdicts
            ]
            store.save_judgments(rows)
            judged += len(verdicts)
    logger.info("Judged {} people in {} batches ({} batches failed)", judged, len(batches), failed_batches)
    return judged
