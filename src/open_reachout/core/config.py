"""Configuration schemas: the Brief and the compiled program (PRD FR-0.x, FR-1.1).

One config system: hand-written and synthesis-generated artifacts use the same
models. ``extra="forbid"`` everywhere — unknown keys are errors, not warnings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from open_reachout import MAX_FOLLOW_UPS, MIN_FOLLOW_UP_GAP_DAYS
from open_reachout.core.variables import validate_prompt


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- brief
class GoalSpec(_Model):
    convert: str = Field(min_length=3, description="conversion definition, plain language")
    brainstorm: str | None = None


class IdentitySpec(_Model):
    sender: str = Field(
        min_length=3, description='real person or honest brand, e.g. "Maya Reyes, StageMatch"'
    )
    physical_address: str = Field(min_length=8)
    disclose_automation: bool = True
    unsubscribe_text: str = Field(
        default="Reply STOP and you'll never hear from us again.",
        min_length=10,
        description=(
            "plain-text opt-out line in every message (one-click is provider-side)"
        ),
    )


class AboutUs(_Model):
    name: str
    what_we_do: str = Field(
        min_length=10, description="the only permitted source of product claims"
    )
    links: dict[str, str] = Field(default_factory=dict)
    identity: IdentitySpec

    @field_validator("links")
    @classmethod
    def _https_links(cls, v: dict[str, str]) -> dict[str, str]:
        for key, url in v.items():
            if not url.startswith("https://"):
                raise ValueError(f"link {key!r} must be https:// (got {url!r})")
        return v


class Budgets(_Model):
    monthly_prospects: int = Field(gt=0, le=100_000)
    monthly_llm_usd: float = Field(gt=0, le=100_000)


Autonomy = Literal["review_everything", "standard", "hands_off"]


class Brief(_Model):
    """The primary authored artifact (PRD FR-0.1)."""

    find: str = Field(min_length=20, description="what kind of users to find")
    research: str = Field(min_length=20, description="what kind of research to do")
    goals: GoalSpec
    about_us: AboutUs
    budgets: Budgets
    autonomy: Autonomy = "standard"


# ------------------------------------------------------------------- program parts
class SequenceSpec(_Model):
    steps: int = Field(ge=1, le=1 + MAX_FOLLOW_UPS, description="opener + follow-ups")
    gaps_days: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _gaps(self) -> SequenceSpec:
        if len(self.gaps_days) != self.steps - 1:
            raise ValueError(f"need {self.steps - 1} gaps for {self.steps} steps")
        if any(g < MIN_FOLLOW_UP_GAP_DAYS for g in self.gaps_days):
            raise ValueError(f"gaps must be >= {MIN_FOLLOW_UP_GAP_DAYS} days")
        return self


class VariantSpec(_Model):
    """A versioned generation prompt — never a template (PRD FR-3.1)."""

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    surface: str
    attributes: dict[str, str] = Field(default_factory=dict)
    prompt: str = Field(min_length=20)

    @field_validator("prompt")
    @classmethod
    def _slots_registered(cls, v: str) -> str:
        unknown = validate_prompt(v)
        if unknown:
            raise ValueError(f"unknown variable slot(s): {', '.join(unknown)}")
        return v


class CohortSpec(_Model):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    filters: dict[str, object] = Field(default_factory=dict)
    monthly_budget: int = Field(gt=0)
    sources: list[str] = Field(min_length=1)


class PersonaSpec(_Model):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    description: str = Field(min_length=20)
    evidence_signals: list[str] = Field(min_length=1)
    value_prop: str = Field(min_length=10)
    voice: dict[str, str] = Field(default_factory=dict)
    sequence: SequenceSpec
    cohorts: list[CohortSpec] = Field(min_length=1)
    variants: list[VariantSpec] = Field(min_length=1)
    priority: int = Field(default=100, description="entity-collision arbitration; lower wins")


class GeneratedBy(_Model):
    agent: str
    config_hash: str


class TenantConfig(_Model):
    tenant: str = Field(pattern=r"^[a-z0-9_-]+$")
    brief: Brief
    personas: list[PersonaSpec] = Field(min_length=1)
    generated_by: GeneratedBy | None = None  # synthesis provenance (FR-0.4)

    @model_validator(mode="after")
    def _budget_consistency(self) -> TenantConfig:
        total = sum(c.monthly_budget for p in self.personas for c in p.cohorts)
        cap = self.brief.budgets.monthly_prospects
        if total > cap:
            raise ValueError(f"cohort budgets sum to {total} > monthly_prospects cap {cap}")
        ids = [p.id for p in self.personas]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate persona ids")
        return self


# ---------------------------------------------------------------------- autonomy
class AutonomyKnobs(_Model):
    reply_actions: Literal["propose", "auto"]
    prompt_variants: Literal["propose", "auto"]
    cohort_launch: Literal["off", "propose", "auto_within_budget"]
    # Hard-coded always-human set (FR-0.3): not represented here on purpose —
    # new personas, value-prop claim changes, spend-cap raises, and halt
    # resume have no knob to turn.


AUTONOMY_PRESETS: dict[str, AutonomyKnobs] = {
    "review_everything": AutonomyKnobs(
        reply_actions="propose", prompt_variants="propose", cohort_launch="propose"
    ),
    "standard": AutonomyKnobs(
        reply_actions="auto", prompt_variants="auto", cohort_launch="propose"
    ),
    "hands_off": AutonomyKnobs(
        reply_actions="auto", prompt_variants="auto", cohort_launch="auto_within_budget"
    ),
}


def expand_autonomy(preset: Autonomy) -> AutonomyKnobs:
    return AUTONOMY_PRESETS[preset]


# ----------------------------------------------------------------------- loading
class ConfigError(Exception):
    pass


def load_tenant(path: Path) -> TenantConfig:
    """Load and validate one tenant config file. Raises ConfigError with all
    pydantic detail attached — fail closed, fail loudly, fail early."""
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at top level")
    try:
        return TenantConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"{path}: {exc}") from exc
