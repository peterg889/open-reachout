"""`reachout dry-run`: the full pipeline through compose, zero sends (FR-1.2).

Runs discover -> enrich -> find/verify -> qualify -> compose for N prospects
per cohort and writes the would-send messages to a review file. With the fake
LLM this exercises plumbing; with the Anthropic backend it produces the real
review artifact operators judge programs by (PRD FR-0.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from open_reachout.agents.composer import (
    ComposeEscalation,
    ComposeInputs,
    ComposeResult,
    compose,
)
from open_reachout.agents.qualifier import qualify
from open_reachout.agents.schemas import ComposeOutput, GroundednessOutput, QualifyOutput
from open_reachout.core.compliance.validators import ValidatorContext
from open_reachout.core.config import PersonaSpec, TenantConfig
from open_reachout.core.evidence import fresh_facts
from open_reachout.core.interfaces import (
    Candidate,
    ConfidenceBucket,
    EmailFinder,
    Enricher,
    EvidenceCard,
    LLMBackend,
    SourceAdapter,
    Verifier,
)
from open_reachout.core.variables import ResolvedValue, TrustClass, extract_slots


def validator_context(tenant: TenantConfig) -> ValidatorContext:
    from open_reachout.core.compliance.claims import registry_version

    about_us = tenant.brief.about_us
    identity = about_us.identity
    return ValidatorContext(
        physical_address=identity.physical_address,
        unsubscribe_text=identity.unsubscribe_text,
        sender_identity=identity.sender,
        # registered collateral URLs are allowlisted (FR-3.10): vetted at
        # registration, so the URL validator lets them through
        allowed_url_prefixes=tuple(about_us.links.values())
        + tuple(a.url for a in about_us.assets),
        claim_mode=about_us.claims_mode,
        approved_claims=tuple(about_us.approved_claims),
        claim_registry_version=registry_version(about_us),
        sector_sensitivity=about_us.sector_sensitivity,
        compliance_regime=about_us.compliance_regime,
    )


def trusted_context(tenant: TenantConfig, persona: PersonaSpec) -> str:
    identity = tenant.brief.about_us.identity
    lines = [
        f"Sender identity line (include verbatim): - {identity.sender}",
        f"Physical address line (include verbatim): {identity.physical_address}",
        f"Unsubscribe line (include verbatim): {identity.unsubscribe_text}",
        f"What we do: {tenant.brief.about_us.what_we_do}",
        f"Value prop for this persona: {persona.value_prop}",
        "Allowed links: "
        + (", ".join(f"{k}: {v}" for k, v in tenant.brief.about_us.links.items()) or "(none)"),
    ]
    if identity.disclose_automation:
        lines.append(
            "Disclosure (work a brief honest version into the message): drafting is "
            "AI-assisted; a human reads every reply."
        )
    return "\n".join(lines)


def build_values(
    prompt: str,
    candidate: Candidate,
    card: EvidenceCard,
    tenant: TenantConfig,
    persona: PersonaSpec,
    sender_facts: dict[str, str] | None = None,
) -> dict[str, ResolvedValue]:
    """Map every slot in the variant prompt to a value with correct trust class.

    Evidence slots draw from fresh facts in order; missing evidence fails
    closed upstream (resolve raises KeyError -> prospect escalates).
    """
    facts = fresh_facts(card)
    values: dict[str, ResolvedValue] = {}
    fact_iter = iter(facts)
    for slot in extract_slots(prompt):
        if slot.startswith("sender."):
            sender_fact = (sender_facts or {}).get(slot[len("sender."):])
            if sender_fact is None:
                continue  # unapproved sender fact: resolve() raises, escalates
            values[slot] = ResolvedValue(slot, sender_fact, TrustClass.TRUSTED)
        elif slot.startswith("asset."):
            asset = next(
                (a for a in tenant.brief.about_us.assets if a.id == slot[len("asset."):]),
                None,
            )
            if asset is None:
                continue  # unknown asset id: resolve() raises, caller escalates
            values[slot] = ResolvedValue(slot, asset.url, TrustClass.TRUSTED)
        elif slot.startswith("evidence."):
            fact = next(fact_iter, None)
            if fact is None:
                continue  # unresolvable -> resolve() raises, caller escalates
            values[slot] = ResolvedValue(
                slot, fact.content, TrustClass.UNTRUSTED, fact_id=fact.fact_id,
                source_url=fact.source_url,
            )
        elif slot == "prospect.first_name":
            values[slot] = ResolvedValue(
                slot, candidate.display_name.split()[0], TrustClass.PROSPECT
            )
        elif slot == "prospect.org_name":
            values[slot] = ResolvedValue(
                slot, candidate.org_name or candidate.display_name, TrustClass.PROSPECT
            )
        elif slot == "persona.value_prop":
            values[slot] = ResolvedValue(slot, persona.value_prop, TrustClass.TRUSTED)
        elif slot == "persona.voice_rules":
            voice = "; ".join(f"{k}: {v}" for k, v in persona.voice.items())
            values[slot] = ResolvedValue(slot, voice or "plain and warm", TrustClass.TRUSTED)
        elif slot.startswith("tenant.links."):
            link = tenant.brief.about_us.links.get(slot.rsplit(".", 1)[1], "")
            values[slot] = ResolvedValue(slot, link, TrustClass.TRUSTED)
        elif slot == "tenant.name":
            values[slot] = ResolvedValue(slot, tenant.brief.about_us.name, TrustClass.TRUSTED)
    return values


@dataclass
class DryRunReport:
    composed: list[tuple[str, str, ComposeResult]] = field(default_factory=list)  # cohort, name
    disqualified: list[tuple[str, str, str]] = field(default_factory=list)  # cohort, name, why
    escalated: list[tuple[str, str, str]] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = ["# Dry-run review", ""]
        lines.append(
            f"{len(self.composed)} would-send drafts, {len(self.disqualified)} disqualified, "
            f"{len(self.escalated)} escalated. Nothing was sent."
        )
        for cohort, name, result in self.composed:
            lines += [
                "",
                f"## [{cohort}] {name}",
                f"**Subject:** {result.draft.subject}",
                "",
                result.draft.body,
                "",
                "*Claims:* "
                + ("; ".join(f"{c.text} [{c.fact_id}]" for c in result.output.claims) or "(none)"),
            ]
        if self.disqualified:
            lines += ["", "## Disqualified"]
            lines += [f"- [{c}] {n}: {why}" for c, n, why in self.disqualified]
        if self.escalated:
            lines += ["", "## Escalated to review"]
            lines += [f"- [{c}] {n}: {why}" for c, n, why in self.escalated]
        return "\n".join(lines) + "\n"


def run(
    tenant: TenantConfig,
    sources: dict[str, SourceAdapter],
    enricher: Enricher,
    finder: EmailFinder,
    verifier: Verifier,
    llm: LLMBackend,
    n_per_cohort: int,
    out_path: Path,
) -> DryRunReport:
    report = DryRunReport()
    ctx = validator_context(tenant)
    for persona in tenant.personas:
        trusted = trusted_context(tenant, persona)
        variant = persona.variants[0]  # bandit selection is meaningless pre-launch
        for cohort in persona.cohorts:
            if cohort.trigger is not None:
                continue  # event-triggered cohorts are dormant until fired (FR-2.9)
            source = next((sources[s] for s in cohort.sources if s in sources), None)
            if source is None:
                continue
            result = source.discover(cohort.filters, None)
            for candidate in result.candidates[:n_per_cohort]:
                _process(
                    report, tenant, persona, cohort.id, candidate,
                    enricher, finder, verifier, llm, variant.prompt, trusted, ctx,
                )
    out_path.write_text(report.to_markdown())
    return report


def _process(  # noqa: PLR0913 — pipeline stage wiring
    report: DryRunReport,
    tenant: TenantConfig,
    persona: PersonaSpec,
    cohort_id: str,
    candidate: Candidate,
    enricher: Enricher,
    finder: EmailFinder,
    verifier: Verifier,
    llm: LLMBackend,
    variant_prompt: str,
    trusted: str,
    ctx: ValidatorContext,
) -> None:
    name = candidate.display_name
    card = enricher.enrich(candidate)
    email = finder.find(candidate)
    if email and verifier.verify(email.email).bucket is not ConfidenceBucket.VERIFIED:
        report.disqualified.append((cohort_id, name, "email failed verification"))
        return
    verdict = qualify(llm, card, persona.description)
    if verdict.escalate:
        report.escalated.append((cohort_id, name, "injection suspicion during qualification"))
        return
    if not verdict.qualified:
        report.disqualified.append((cohort_id, name, verdict.rationale))
        return
    try:
        values = build_values(variant_prompt, candidate, card, tenant, persona)
        result = compose(
            llm,
            ComposeInputs(
                variant_id="dryrun",
                variant_prompt=variant_prompt,
                values=values,
                validator_ctx=ctx,
                trusted_context=trusted,
            ),
        )
    except (ComposeEscalation, KeyError) as exc:
        report.escalated.append((cohort_id, name, str(exc)))
        return
    report.composed.append((cohort_id, name, result))


class ScriptedLLM:
    """Deterministic stand-in LLM for fake-mode dry runs and the e2e harness.

    Honest scaffolding: it exercises every pipeline contract (schemas, claims,
    validators, groundedness plumbing) without a model. Real review artifacts
    come from --llm anthropic.
    """

    def __init__(self, ctx: ValidatorContext, sender: str) -> None:
        self.ctx = ctx
        self.sender = sender

    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        if task == "qualify":
            return QualifyOutput(verdict="qualified", rationale="scripted: matches persona")
        if task == "groundedness":
            return GroundednessOutput(grounded=True)
        if task == "compose":
            fact_id = _first_fact_id(prompt)
            claims = (
                [{"text": "referenced evidence", "fact_id": fact_id}] if fact_id else []
            )
            body = (
                "Hi - I noticed your work and thought this might genuinely fit. "
                "Drafting here is AI-assisted; a human reads every reply.\n\n"
                f"- {self.sender}\n{self.ctx.physical_address}\n{self.ctx.unsubscribe_text}"
            )
            return ComposeOutput(subject="A small, honest pitch", body=body, claims=claims)
        raise KeyError(f"ScriptedLLM has no script for task {task!r}")


def _first_fact_id(prompt: str) -> str | None:
    for line in prompt.splitlines():
        if line.startswith("fact_id="):
            return line.removeprefix("fact_id=").strip()
    return None
