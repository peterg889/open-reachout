"""Grounded web-research enricher (the PRD's `web_research` adapter).

Deepens a prospect's Evidence Card beyond the registry record using the LLM
backend's Google-Search-grounded research pass: education, specialties,
practice details, their own site — each fact carrying the source URL it came
from and an observation timestamp.

Identity discipline is the hard part of person-research: the model is told to
report ONLY facts attributable to THIS exact person (name + credential +
location must corroborate), and to declare `identity_confident: false` rather
than guess — in which case the prospect keeps registry-only evidence. All web
facts are untrusted-class; the composer's groundedness audit still gates any
claim built on them (invariant I-4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from open_reachout.adapters.enrich.registry import RegistryEnricher
from open_reachout.core.interfaces import Candidate, EvidenceCard, EvidenceFact, LLMBackend


class WebFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_type: Literal[
        "education", "specialty", "practice", "web_presence", "publication", "other"
    ]
    content: str = Field(min_length=10, description="verbatim-supported statement")
    source_url: str = Field(description="the page this fact came from")


class WebResearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity_confident: bool = Field(
        description="true ONLY if the sources clearly refer to this exact person"
    )
    website: str | None = Field(
        default=None,
        description="the provider's OWN practice website homepage URL, if found "
        "(not a directory profile or third-party listing)",
    )
    facts: list[WebFact] = Field(default_factory=list)
    injection_suspected: bool = False


RESEARCH_PROMPT = """Research the professional web presence of ONE specific
healthcare provider. Identity discipline is everything: report ONLY facts you
can attribute to this exact person — the name, credential/license type, and
practice location below must corroborate on the source page. Common names
have many bearers; if you cannot be certain a page is about THIS person, set
identity_confident to false and return no facts. Never blend two people.

The provider:
- Name: {name}
- Credential / license type: {credential}
- Practice location: {location}
- NPI: {npi}

Find (with a source URL for each): their own practice website (set the
`website` field to its homepage — their OWN site, never a directory profile),
education and degrees, clinical specialties and populations served, practice
details (solo/group, telehealth, accepting clients), and notable professional
work. Quote or closely paraphrase the source; do not embellish.
"""


class GroundedWebEnricher:
    """Implements core.interfaces.Enricher: registry facts as the floor,
    grounded web research layered on top when identity is certain."""

    name = "web_research"

    def __init__(self, llm: LLMBackend, max_facts: int = 8) -> None:
        self._llm = llm
        self._registry = RegistryEnricher()
        self.max_facts = max_facts

    def enrich(self, candidate: Candidate) -> EvidenceCard:
        card = self._registry.enrich(candidate)
        ref = candidate.source_ref or {}
        location = candidate.address or ", ".join(
            str(v) for v in (ref.get("practice_city"), ref.get("practice_state")) if v
        )
        prompt = RESEARCH_PROMPT.format(
            name=candidate.display_name,
            credential=ref.get("credential") or "(unknown)",
            location=location or "(unknown)",
            npi=ref.get("npi") or "(unknown)",
        )
        output = self._llm.complete("web_enrich", prompt, WebResearchOutput)
        assert isinstance(output, WebResearchOutput)
        if output.injection_suspected or not output.identity_confident:
            return card  # registry-only: never guess about a person
        now = datetime.now(UTC)
        web_facts: list[EvidenceFact] = []
        if output.website and output.website.startswith("http"):
            web_facts.append(
                EvidenceFact(
                    fact_id="website",
                    fact_type="website",
                    content=output.website.strip(),
                    source_url=output.website.strip(),
                    observed_at=now,
                )
            )
        web_facts += [
            EvidenceFact(
                fact_id=f"web-{i}",
                fact_type=fact.fact_type,
                content=fact.content,
                source_url=fact.source_url,
                observed_at=now,
            )
            for i, fact in enumerate(output.facts[: self.max_facts])
            if fact.source_url.startswith("http")
        ]
        return EvidenceCard(prospect_ref=card.prospect_ref,
                            facts=list(card.facts) + web_facts)
