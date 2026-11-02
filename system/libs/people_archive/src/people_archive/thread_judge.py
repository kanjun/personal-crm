"""Read the text thread behind an unnamed number and say who is there.

A number the user texted one-to-one but never saved shows up in the people list as a bare phone
number. The thread itself usually says who it is: a friend who opened with "this is Vincent", a
colleague coordinating an offsite, a store's marketing line, a courier, a verification code. This
module shows the model the thread and records, per handle: whether it is someone the user knows
(warm), a stranger or one-off transaction (cold), a bot (automated), or unclear; the name when the
thread reveals it; and one clause on who they are. The identity pass reads the result: warm names
replace the bare number, and cold or automated threads leave the people list.
"""

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
from people_archive.primitives import HandleKey
from people_archive.store import ArchiveStore, ThreadJudgmentRow

THREAD_JUDGE_MODEL: Final[str] = "claude-sonnet-5"
THREADS_PER_CALL: Final[int] = 6
# Each call is a separate claude process (~300 MB); more than a few at once gets them killed for memory.
_WORKERS: Final[int] = 2
_OPENING_MESSAGES: Final[int] = 12
_CLOSING_MESSAGES: Final[int] = 8
_MESSAGE_CHARS: Final[int] = 160
_GROUP_CHATS_SHOWN: Final[int] = 4

_SYSTEM_PROMPT: Final[str] = """You read text-message threads from the user's phone. Each numbered entry is a number the user has
texted one-to-one but never saved as a contact. You see the opening and closing messages ("the user:" is the user's,
"Them:" is the other side), the years the thread spans, and any group chats the number also sits in.
Decide who is behind the number:

- warm: a person the user knows. A friend, colleague, family member, investor, founder, or acquaintance with a
  genuine two-way personal or professional conversation (plans, favors, opinions, follow-ups, shared people).
  A short thread still counts as warm when it is clearly a real relationship ("this is Vincent!", "great to
  see you last night").
- cold: a real human but a stranger or a one-off transaction: a recruiter or salesperson pitching, a driver,
  a host or vendor for one event, a customer-service rep, a landlord's handyman, a wrong number.
- automated: marketing blasts, verification codes, delivery and appointment notifications, surveys, any
  sender that is a system or a brand rather than a person.
- unsure: too little to tell.

Also give:
- name: the person's name if the thread reveals it (they introduce themselves, sign off, the user addresses
  them, a group chat lists them). Use the form the thread uses, e.g. "Vincent" or "Sam Lee". Empty if unknown.
  Never invent one.
- who: one short clause on who they are and how they connect to the user, from the thread only.
- confidence: 0.0-1.0.

Return ONLY a JSON array, one object per entry, same order:
{"n": <number>, "kind": "warm" | "cold" | "automated" | "unsure", "name": "...", "who": "...", "confidence": <0.0-1.0>}"""

_KINDS: Final[frozenset[str]] = frozenset({"warm", "cold", "automated", "unsure"})


class ThreadJudgeError(PeopleArchiveError):
    """The judge's answer could not be read."""


class _JudgmentLine(FrozenModel):
    """One entry of the judge's JSON answer."""

    n: int = Field(description="Entry number")
    kind: str = Field(description="warm / cold / automated / unsure")
    name: str = Field(default="", description="Name if revealed")
    who: str = Field(default="", description="Who they are")
    confidence: float = Field(default=0.0, description="0..1")


class _Thread(FrozenModel):
    """One unnamed number with the evidence the prompt shows."""

    handle_key: HandleKey = Field(description="The phone or chat handle")
    evidence: str = Field(description="Evidence lines for the prompt")


@pure
def _day(ts: Any) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")


@pure
def _sample(messages: Sequence[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """The opening and closing of a thread; the middle of a long one adds little."""
    if len(messages) <= _OPENING_MESSAGES + _CLOSING_MESSAGES:
        return list(messages)
    return [*messages[:_OPENING_MESSAGES], *messages[-_CLOSING_MESSAGES:]]


def unnamed_handles(store: ArchiveStore) -> list[HandleKey]:
    """Handles of listed people who are nothing but a phone number or chat handle with a one-to-one thread."""
    rows = store.execute_read(
        "SELECT h.handle_key FROM persons p JOIN person_handles h ON h.person_id = p.person_id "
        "WHERE p.is_human = 1 AND p.emails_json = '[]' AND h.handle_key LIKE 'phone:%' AND p.canonical_name = substr(h.handle_key, 7) "
        "AND EXISTS (SELECT 1 FROM beeper_participants bp JOIN beeper_chats c ON c.chat_id = bp.chat_id "
        "            WHERE bp.handle_key = h.handle_key AND c.chat_type = 'single') ORDER BY p.score DESC",
        [],
    )
    return [HandleKey(str(r[0])) for r in rows]


def _thread_for(store: ArchiveStore, handle_key: HandleKey) -> _Thread | None:
    chat_ids = [str(r[0]) for r in store.execute_read(
        "SELECT bp.chat_id FROM beeper_participants bp JOIN beeper_chats c ON c.chat_id = bp.chat_id "
        "WHERE bp.handle_key = ? AND c.chat_type = 'single' ORDER BY c.last_activity_ts DESC", [handle_key],
    )]
    if not chat_ids:
        return None
    placeholders = ", ".join("?" * len(chat_ids))
    messages = store.execute_read(
        f"SELECT ts, is_sender, text FROM beeper_messages WHERE chat_id IN ({placeholders}) AND text != '' ORDER BY ts", chat_ids,
    )
    if not messages:
        return None
    lines = [f"thread: {len(messages)} messages, {_day(messages[0][0])} to {_day(messages[-1][0])}"]
    groups = store.execute_read(
        "SELECT c.network, c.title, bp.full_name FROM beeper_participants bp JOIN beeper_chats c ON c.chat_id = bp.chat_id "
        f"WHERE bp.handle_key = ? AND c.chat_type = 'group' ORDER BY c.last_activity_ts DESC LIMIT {_GROUP_CHATS_SHOWN}", [handle_key],
    )
    for network, title, full_name in groups:
        lines.append(f"also in {network} group \"{str(title)[:50]}\"" + (f" as {full_name}" if full_name else ""))
    for ts, is_sender, text in _sample(messages):
        speaker = "the user" if is_sender else "Them"
        lines.append(f"{_day(ts)} {speaker}: {' '.join(str(text).split())[:_MESSAGE_CHARS]}")
    return _Thread(handle_key=handle_key, evidence="\n".join(lines))


@pure
def _prompt_for(threads: Sequence[_Thread]) -> str:
    return "Entries:\n\n" + "\n\n".join(f"{idx + 1}. {thread.handle_key}\n{thread.evidence}" for idx, thread in enumerate(threads))


@pure
def _parse(text: str, expected: int) -> list[_JudgmentLine]:
    stripped = text.strip()
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start < 0 or end < start:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end < start:
            raise ThreadJudgeError(f"No JSON array: {stripped[:100]!r}")
        stripped = f"[{stripped[start:end + 1]}]"
        start, end = 0, len(stripped) - 1
    try:
        lines = [_JudgmentLine.model_validate(item) for item in json.loads(stripped[start:end + 1])]
    except (json.JSONDecodeError, ValidationError) as e:
        raise ThreadJudgeError(f"Unreadable answer: {e}") from e
    if len(lines) != expected:
        raise ThreadJudgeError(f"Answered {len(lines)} of {expected}")
    return lines


def judge_batch(threads: Sequence[_Thread], model: str) -> list[ThreadJudgmentRow]:
    try:
        result = claude_p_completion(_prompt_for(threads), system=_SYSTEM_PROMPT, model=model)
    except ClaudeCLIError as e:
        raise ThreadJudgeError(f"Thread judge call failed: {e}") from e
    judged_at = datetime.now(timezone.utc)
    rows: list[ThreadJudgmentRow] = []
    for line in _parse(result.text, len(threads)):
        if not 1 <= line.n <= len(threads):
            continue
        kind = line.kind.strip().lower()
        rows.append(ThreadJudgmentRow(
            handle_key=threads[line.n - 1].handle_key, kind=kind if kind in _KINDS else "unsure",
            name=line.name.strip()[:80], who=line.who.strip()[:200], confidence=min(1.0, max(0.0, line.confidence)),
            judged_at=judged_at, model=model,
        ))
    return rows


class ThreadJudgeSummary(FrozenModel):
    """What one run did."""

    judged: int = Field(description="Threads judged now")
    warm: int = Field(description="People the user knows")
    named: int = Field(description="Of those, with a name read from the thread")
    cold: int = Field(description="Strangers and one-off transactions")
    automated: int = Field(description="Bots and brands")
    unsure: int = Field(description="Could not tell")


def judge_unnamed_threads(store: ArchiveStore, model: str, limit: int, rejudge: bool = False) -> ThreadJudgeSummary:
    """Judge the threads of listed people who are only a number; results apply at the next identity pass."""
    judged = set() if rejudge else set(store.thread_judgments())
    threads = []
    for handle_key in unnamed_handles(store):
        if handle_key in judged:
            continue
        thread = _thread_for(store, handle_key)
        if thread is not None:
            threads.append(thread)
        if len(threads) >= limit:
            break
    batches = [threads[i:i + THREADS_PER_CALL] for i in range(0, len(threads), THREADS_PER_CALL)]
    counts = {"judged": 0, "warm": 0, "named": 0, "cold": 0, "automated": 0, "unsure": 0}

    def run(batch: Sequence[_Thread]) -> list[ThreadJudgmentRow]:
        try:
            return judge_batch(batch, model)
        except ThreadJudgeError as e:
            logger.warning("Thread judge batch skipped: {}", e)
            return []

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for rows in pool.map(run, batches):
            if not rows:
                continue
            store.save_thread_judgments(rows)
            for row in rows:
                counts["judged"] += 1
                counts[row.kind] += 1
                if row.kind == "warm" and row.name:
                    counts["named"] += 1
    logger.info("Judged {} threads in {} batches: {} warm ({} named), {} cold, {} automated, {} unsure",
                counts["judged"], len(batches), counts["warm"], counts["named"], counts["cold"], counts["automated"], counts["unsure"])
    return ThreadJudgeSummary(**counts)
