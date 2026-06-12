"""Structured output schemas for LLM tasks (spec 9.1-9.2).

Every envelope-bearing schema carries `injection_suspected` (spec 9.3). All
schemas use extra='forbid': an out-of-schema field is a failed parse, and a
failed parse is a failed job — never a silently-coerced action.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    injection_suspected: bool = False


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(description="the prospect-specific factual claim made in the message")
    fact_id: str = Field(description="the Evidence Card fact this claim cites")


class ComposeOutput(_Out):
    subject: str
    body: str
    claims: list[Claim] = Field(default_factory=list)


class QualifyOutput(_Out):
    verdict: Literal["qualified", "disqualified", "uncertain"]
    rationale: str
    signal_scores: dict[str, float] = Field(default_factory=dict)


class GroundednessOutput(_Out):
    grounded: bool
    unsupported_claims: list[str] = Field(default_factory=list)


class ClassifyReplyOutput(_Out):
    intent: Literal[
        "interested",
        "question",
        "objection",
        "not_interested",
        "unsubscribe",
        "out_of_office",
        "wrong_person",
        "hostile",
        "other",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    #: FR-4.3 objection taxonomy; set when intent == "objection".
    objection_class: Literal["price", "trust", "timing", "already_solved", "other"] | None = (
        None
    )
    sentiment: float = Field(ge=-1.0, le=1.0, default=0.0)
