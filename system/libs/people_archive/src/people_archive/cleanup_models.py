"""Validated GPT outputs for the reversible identity cleanup."""
from typing import Literal

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import ConfigDict, Field

from people_archive.errors import PeopleArchiveError


class CleanupError(PeopleArchiveError):
    """Cleanup cannot safely continue."""


class ProviderUnavailable(CleanupError):
    """GPT authentication, quota or provider failure; never switch providers."""


class Citation(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str
    quote: str = Field(min_length=8)


class Fact(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    field: Literal["name", "email", "phone", "profile", "organization", "title", "location", "context"]
    value: str
    confidence: Literal["high", "moderate", "low"]
    citations: list[Citation]


class Extraction(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    facts: list[Fact]
    needs_more: bool
    reason: str


class PairJudgment(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    decision: Literal["SAME", "DIFFERENT", "UNSURE"]
    confidence: Literal["high", "moderate", "low"]
    basis: Literal["explicit_link", "corroborated_context", "name_only", "conflicting_identity", "insufficient"]
    citations: list[Citation]
    reason: str
    needs_more: bool


class PublicResearch(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    facts: list[Fact]
    reason: str


class ProfileExtraction(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    person_id: str
    extraction: Extraction


class ExtractionBatch(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    people: list[ProfileExtraction]


class PersonResearch(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    person_id: str
    research: PublicResearch


class ResearchBatch(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    people: list[PersonResearch]


class PairResult(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pair_id: str
    judgment: PairJudgment


class JudgmentBatch(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pairs: list[PairResult]
