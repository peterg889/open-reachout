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


class AssetSpec(_Model):
    """FR-3.10 collateral: an operator-vetted file/page referenced in prompts
    as {{asset.<id>}}. `summary` is the asset's claim content, claims-linted
    at registration — collateral cannot smuggle claims past the validators."""

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    url: str
    summary: str = Field(min_length=10, description="what the asset claims, linted")

    @field_validator("url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError(f"asset url must be https:// (got {v!r})")
        return v


class AboutUs(_Model):
    name: str
    what_we_do: str = Field(
        min_length=10, description="the only permitted source of product claims"
    )
    links: dict[str, str] = Field(default_factory=dict)
    identity: IdentitySpec
    assets: list[AssetSpec] = Field(default_factory=list)
    claims_mode: Literal["denylist", "allowlist"] = "denylist"
    sector_sensitivity: Literal["none", "healthcare"] = Field(
        default="none",
        description="FR-3.11: healthcare turns on the PHI screen over outbound "
        "content and operator payloads — client information never transits",
    )
    compliance_regime: str = Field(
        default="us_can_spam",
        description="FR-7.7: jurisdiction regime; extras compose additively "
        "onto the non-bypassable core pack",
    )

    @field_validator("compliance_regime")
    @classmethod
    def _known_regime(cls, v: str) -> str:
        from open_reachout.core.compliance.regimes import get_regime

        try:
            get_regime(v)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        return v
    approved_claims: list[str] = Field(
        default_factory=list,
        description="FR-3.2 allowlist: canonical, versioned product-claim phrases; "
        "in allowlist mode any claim-like sentence must contain one of these",
    )

    @model_validator(mode="after")
    def _allowlist_needs_claims(self) -> AboutUs:
        if self.claims_mode == "allowlist" and not self.approved_claims:
            raise ValueError("claims_mode: allowlist requires at least one approved claim")
        if any(len(c.strip()) < 5 for c in self.approved_claims):
            raise ValueError("approved claims must be substantive phrases (>= 5 chars)")
        return self

    @model_validator(mode="after")
    def _assets_claims_linted(self) -> AboutUs:
        """FR-3.10: collateral is claims-linted at registration."""
        from open_reachout.core.compliance.validators import (
            forbidden_hits,
            unregistered_claims,
        )

        ids = [a.id for a in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate asset ids")
        for asset in self.assets:
            hits = forbidden_hits(asset.summary)
            if hits:
                raise ValueError(f"asset {asset.id!r}: forbidden claim(s): {hits}")
            if self.claims_mode == "allowlist":
                flagged = unregistered_claims(asset.summary, tuple(self.approved_claims))
                if flagged:
                    raise ValueError(
                        f"asset {asset.id!r}: unregistered claim(s): {flagged}"
                    )
        return self

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
    human_tasks: dict[int, str] = Field(
        default_factory=dict,
        description="step_index -> off-channel instruction (FR-3.6); the framework "
        "briefs, the operator does the touch ('DM them on Instagram')",
    )

    @model_validator(mode="after")
    def _gaps(self) -> SequenceSpec:
        if len(self.gaps_days) != self.steps - 1:
            raise ValueError(f"need {self.steps - 1} gaps for {self.steps} steps")
        if any(g < MIN_FOLLOW_UP_GAP_DAYS for g in self.gaps_days):
            raise ValueError(f"gaps must be >= {MIN_FOLLOW_UP_GAP_DAYS} days")
        for step, instruction in self.human_tasks.items():
            if not 0 <= step < self.steps:
                raise ValueError(f"human_tasks step {step} outside 0..{self.steps - 1}")
            if len(instruction.strip()) < 10:
                raise ValueError(f"human_tasks[{step}]: instruction too short to act on")
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


class TriggerSpec(_Model):
    """Event-triggered cohort (FR-2.9): activated by matching operator events
    (`POST /v1/events`) instead of the discovery cadence."""

    event_type: str = Field(pattern=r"^[a-z0-9_.:-]+$")


class FloorSpec(_Model):
    """FR-6.5 underperformance floors. A cohort is flagged only when the
    one-sided 90% Wilson upper bound of its realized rate sits below the floor
    with at least `min_trials` — small-sample noise cannot trigger a shift."""

    conversion_rate: float | None = Field(default=None, ge=0, le=1)
    reply_rate: float | None = Field(default=None, ge=0, le=1)
    min_trials: int = Field(default=25, ge=5)


class CohortSpec(_Model):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    filters: dict[str, object] = Field(default_factory=dict)
    monthly_budget: int = Field(gt=0)
    sources: list[str] = Field(min_length=1)
    trigger: TriggerSpec | None = None
    floors: FloorSpec | None = None


class ReferralSpec(_Model):
    """FR-4.4: one referral ask per entity, ever, gated on positive signal.
    `direct` sends the ask ourselves (REPLY gate profile — it continues an
    existing positive conversation); `on_behalf_of` drafts a colleague invite
    delivered TO the converted provider as a human task — the framework never
    sends as them (FR-3.8 makes a forged peer From unrepresentable)."""

    prompt: str = Field(min_length=20)
    mode: Literal["direct", "on_behalf_of"] = "direct"

    @field_validator("prompt")
    @classmethod
    def _slots_registered(cls, v: str) -> str:
        unknown = validate_prompt(v)
        if unknown:
            raise ValueError(f"unknown variable slot(s): {', '.join(unknown)}")
        return v


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
    approve_first: int = Field(
        default=0, ge=0,
        description="FR-0.3 message-review ramp: hold the first N drafts per "
        "(campaign, variant) for per-message approval, then autopilot",
    )
    referral: ReferralSpec | None = None
    objection_counters: dict[str, str] = Field(
        default_factory=dict,
        description="FR-4.3: per-class operator-approved counter-snippets the "
        "reply agent may use in its single agentic exchange; claims-linted",
    )
    reengagement_prompt: str | None = Field(
        default=None,
        description="FR-4.5: generation prompt for the single post-no-show "
        "re-engagement; when unset, a missed booking escalates to a human",
    )

    @field_validator("reengagement_prompt")
    @classmethod
    def _reengage_slots(cls, v: str | None) -> str | None:
        if v is not None:
            unknown = validate_prompt(v)
            if unknown:
                raise ValueError(f"unknown variable slot(s): {', '.join(unknown)}")
        return v

    @field_validator("objection_counters")
    @classmethod
    def _counters_linted(cls, v: dict[str, str]) -> dict[str, str]:
        from open_reachout.core.compliance.validators import forbidden_hits

        allowed = {"price", "trust", "timing", "already_solved", "other"}
        for klass, snippet in v.items():
            if klass not in allowed:
                raise ValueError(f"unknown objection class {klass!r} (allowed: {allowed})")
            hits = forbidden_hits(snippet)
            if hits:
                raise ValueError(f"objection counter {klass!r}: forbidden claim(s): {hits}")
        return v


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


def load_brief(path: Path) -> Brief:
    """Load a Brief from a bare brief.yaml, or extract one from a tenant.yaml
    (anything with a top-level `brief:` key)."""
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at top level")
    payload = raw.get("brief", raw)
    try:
        return Brief.model_validate(payload)
    except Exception as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def dump_tenant(cfg: TenantConfig) -> str:
    """Serialize a tenant config to YAML (synthesis output, FR-0.4)."""
    return yaml.safe_dump(
        cfg.model_dump(mode="json", exclude_none=True), sort_keys=False, width=88
    )


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
