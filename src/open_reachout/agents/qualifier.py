"""LLM qualifier (PRD FR-2.7, spec 8.3): uncertain -> disqualified, always."""

from __future__ import annotations

from dataclasses import dataclass

from open_reachout.agents.prompts import QUALIFY_FRAME
from open_reachout.agents.schemas import QualifyOutput
from open_reachout.core.interfaces import EvidenceCard, LLMBackend
from open_reachout.security.envelope import wrap


@dataclass(frozen=True)
class QualifyResult:
    qualified: bool
    verdict: str  # raw model verdict, kept for the audit trail
    rationale: str
    escalate: bool  # injection suspicion -> human review + source tagging


def qualify(llm: LLMBackend, card: EvidenceCard, persona_description: str) -> QualifyResult:
    evidence = "\n\n".join(
        f"fact_id={f.fact_id} ({f.fact_type}, {f.source_url})\n"
        f"{wrap(f.content, source='web', idem=f.fact_id).text}"
        for f in card.facts
    )
    output = llm.complete(
        "qualify",
        QUALIFY_FRAME.format(persona=persona_description, evidence_blocks=evidence or "(none)"),
        QualifyOutput,
    )
    assert isinstance(output, QualifyOutput)
    # Precision over recall (FR-2.7): only an explicit 'qualified' passes.
    return QualifyResult(
        qualified=output.verdict == "qualified" and not output.injection_suspected,
        verdict=output.verdict,
        rationale=output.rationale,
        escalate=output.injection_suspected,
    )
