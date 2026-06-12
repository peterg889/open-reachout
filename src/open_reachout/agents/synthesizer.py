"""Program synthesis: Brief -> compiled program (PRD FR-0.2, spec 8.8).

A compiler with an LLM front-end: the model proposes personas/cohorts/variant
prompts, and the ordinary config schemas are the enforcement — synthesized
output that fails `TenantConfig` validation fails synthesis (retry with the
validation errors as feedback, then escalate). The synthesizer cannot exceed
budgets, raise follow-up caps, or reference unregistered variable slots,
because those constraints live in the schemas, not in the prompt.
"""

from __future__ import annotations

import hashlib

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from open_reachout.core.config import (
    Brief,
    CohortSpec,
    GeneratedBy,
    PersonaSpec,
    SequenceSpec,
    TenantConfig,
    VariantSpec,
)
from open_reachout.core.interfaces import LLMBackend
from open_reachout.core.variables import DEFAULT_REGISTRY

MAX_SYNTHESIS_RETRIES = 2

SYNTHESIZER_VERSION = "0.1.0"


class SynthesizedProgram(BaseModel):
    """LLM output schema: PersonaSpec's own validators (slot registry,
    sequence caps, id patterns) are the first enforcement layer."""

    model_config = ConfigDict(extra="forbid")
    personas: list[PersonaSpec]


class SynthesisEscalation(Exception):
    """No valid program after retries; a human gets the partial + errors."""


SYNTHESIS_FRAME = """You compile an outreach Brief into a complete program for
the Open Reachout framework. Output personas, each with cohorts and variant
generation prompts.

Hard constraints (the harness validates these; violations are rejected):
- Product claims come ONLY from the Brief's about_us — never invent features,
  pricing, or guarantees.
- The sum of all cohort monthly_budget values must be <= {monthly_cap}.
- Sequences: at most 3 steps total (opener + 2 follow-ups), gaps >= 3 days.
- Variant prompts are GENERATION PROMPTS for an email-writing model, not
  templates. They may reference only these variable slots: {slots}
- Every variant prompt must direct the writer to use specific evidence
  (an evidence.* slot) — no generic flattery.
- ids: lowercase snake_case.

Design guidance:
- 1-3 personas matching the Brief's `find`; 1-2 cohorts each, sized
  conservatively within budget; 1-2 variants per persona with distinct
  attribute tags (tone/hook/cta) so the bandit has something to learn.
- evidence_signals should be checkable from a prospect's own web presence,
  guided by the Brief's `research` directions.

Brief:
{brief_yaml}
"""


def _frame(brief: Brief) -> str:
    return SYNTHESIS_FRAME.format(
        monthly_cap=brief.budgets.monthly_prospects,
        slots=", ".join(sorted(DEFAULT_REGISTRY)),
        brief_yaml=yaml.safe_dump(brief.model_dump(mode="json"), sort_keys=False),
    )


def synthesize(llm: LLMBackend, brief: Brief, tenant_slug: str) -> TenantConfig:
    """Brief -> validated TenantConfig with provenance, or SynthesisEscalation."""
    feedback = ""
    errors: list[str] = []
    for _attempt in range(1 + MAX_SYNTHESIS_RETRIES):
        try:
            program = llm.complete(
                "synthesize_program", _frame(brief) + feedback, SynthesizedProgram
            )
            assert isinstance(program, SynthesizedProgram)
            return assemble(brief, tenant_slug, program.personas)
        except (ValidationError, ValueError) as exc:
            errors.append(str(exc))
            feedback = (
                "\n\nYour previous program failed validation. Fix ALL of the "
                f"following and regenerate the full program:\n{exc}"
            )
    raise SynthesisEscalation(
        f"no valid program after {MAX_SYNTHESIS_RETRIES + 1} attempts: {errors[-1]}"
    )


def assemble(brief: Brief, tenant_slug: str, personas: list[PersonaSpec]) -> TenantConfig:
    """Build the tenant config; TenantConfig validation (budget consistency,
    duplicate ids) is the final enforcement layer."""
    content_hash = hashlib.sha256(
        yaml.safe_dump([p.model_dump(mode="json") for p in personas]).encode()
    ).hexdigest()[:16]
    return TenantConfig(
        tenant=tenant_slug,
        brief=brief,
        personas=personas,
        generated_by=GeneratedBy(
            agent=f"synthesizer@{SYNTHESIZER_VERSION}", config_hash=content_hash
        ),
    )


def template_program(brief: Brief, tenant_slug: str) -> TenantConfig:
    """Deterministic scaffold for fake-mode `reachout init`: one honest,
    valid starting program derived from the Brief — a template to edit, not
    a synthesis. Live synthesis comes from --llm gemini|anthropic."""
    budget = max(brief.budgets.monthly_prospects // 2, 1)
    persona = PersonaSpec(
        id="primary_audience",
        description=(
            f"Derived from the Brief; refine before launch. Find: {brief.find.strip()}"
        )[:500],
        evidence_signals=["has_own_website", "matches_brief_description"],
        value_prop=brief.about_us.what_we_do.strip()[:200],
        voice={"tone": "warm_plain", "register": "peer"},
        sequence=SequenceSpec(steps=3, gaps_days=[4, 7]),
        cohorts=[
            CohortSpec(
                id="launch_cohort",
                filters={"refine_me": "set real filters before launch"},
                monthly_budget=budget,
                sources=["web_research"],
            )
        ],
        variants=[
            VariantSpec(
                id="opener_evidence_first",
                surface="opener_strategy",
                attributes={"tone": "warm", "hook": "their_work", "cta": "reply_question"},
                prompt=(
                    "Write a first-touch email to {{prospect.first_name}} at "
                    "{{prospect.org_name}}. Open with a specific, genuine "
                    "observation about {{evidence.notable_fact}} - never generic "
                    "flattery. One sentence on {{persona.value_prop}}. Close with "
                    "a single easy question. {{persona.voice_rules}}"
                ),
            ),
            VariantSpec(
                id="opener_direct_offer",
                surface="opener_strategy",
                attributes={"tone": "direct", "hook": "value_prop", "cta": "signup_link"},
                prompt=(
                    "Write a short, direct first-touch email to "
                    "{{prospect.first_name}}. Reference {{evidence.notable_fact}} "
                    "in one clause. State {{persona.value_prop}} plainly and "
                    "offer the signup link {{tenant.links.signup}} with zero "
                    "pressure. {{persona.voice_rules}}"
                ),
            ),
        ],
    )
    return assemble(brief, tenant_slug, [persona])
