import os
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from enum import auto
from pathlib import Path
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import Field, ValidationError

from people_archive.primitives import HandleKey

# the user's decisions about who is who: append-only, one JSON object per line, never rewritten by
# the pipeline. Each line carries the handles on both sides so it survives any rebuild of ids.
DEFAULT_ASSERTIONS_PATH: Final[Path] = Path("data/.apps/crm/identity_assertions.jsonl")


def assertions_path() -> Path:
    """The ledger in use: next to the archive the CRM points at, else the default."""
    data_dir = os.environ.get("CRM_DATA_DIR")
    return Path(data_dir) / "identity_assertions.jsonl" if data_dir else DEFAULT_ASSERTIONS_PATH


# Decisions the merge judge made with enough confidence, in the same format. Kept apart from the user's
# own ledger so hers always outrank these and so the whole file can be thrown away to undo the machine's calls.
DEFAULT_AUTO_DECISIONS_PATH: Final[Path] = Path("data/.apps/crm/identity_auto_decisions.jsonl")


def auto_decisions_path() -> Path:
    data_dir = os.environ.get("CRM_DATA_DIR")
    return Path(data_dir) / "identity_auto_decisions.jsonl" if data_dir else DEFAULT_AUTO_DECISIONS_PATH


class Decision(UpperCaseStrEnum):
    """What the user said about two people."""

    SAME = auto()
    DIFFERENT = auto()


class IdentityAssertion(FrozenModel):
    """One recorded decision: these two sets of handles are (or are not) one person."""

    decision: Decision = Field(description="Same or different")
    handles_a: tuple[HandleKey, ...] = Field(description="Every handle of the first person at decision time")
    handles_b: tuple[HandleKey, ...] = Field(description="Every handle of the second person at decision time")
    decided_at: datetime = Field(description="When")
    note: str = Field(default="", description="Optional reason")


def append_assertion(path: Path, assertion: IdentityAssertion) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(assertion.model_dump_json() + "\n")


def load_assertions(path: Path) -> list[IdentityAssertion]:
    if not path.exists():
        return []
    loaded: list[IdentityAssertion] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            loaded.append(IdentityAssertion.model_validate_json(line))
        except ValidationError:
            continue
    return loaded


def make_assertion(decision: Decision, handles_a: Sequence[str], handles_b: Sequence[str], note: str) -> IdentityAssertion:
    return IdentityAssertion(
        decision=decision, handles_a=tuple(HandleKey(h) for h in handles_a), handles_b=tuple(HandleKey(h) for h in handles_b),
        decided_at=datetime.now(timezone.utc), note=note,
    )


@pure
def is_decided(assertion: IdentityAssertion, handles_a: Iterable[str], handles_b: Iterable[str]) -> bool:
    """Whether this assertion already settles the pair of people holding these handles.

    A SAME decision settles any pair drawn from the people it joined; a DIFFERENT decision settles only
    the two sides it kept apart.
    """
    side_a = set(handles_a)
    side_b = set(handles_b)
    asserted_a = set(assertion.handles_a)
    asserted_b = set(assertion.handles_b)
    if assertion.decision == Decision.SAME:
        joined = asserted_a | asserted_b
        return bool(side_a & joined) and bool(side_b & joined)
    return bool(side_a & asserted_a and side_b & asserted_b) or bool(side_a & asserted_b and side_b & asserted_a)
