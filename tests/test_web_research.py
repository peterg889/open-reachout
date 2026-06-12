"""Grounded web-research enricher: identity discipline + fact layering."""

from __future__ import annotations

from pydantic import BaseModel

from open_reachout.adapters.enrich.web_research import GroundedWebEnricher
from open_reachout.core.interfaces import Candidate, DataBasis


def _candidate() -> Candidate:
    return Candidate(
        display_name="Muhammad Abbass", org_name=None, website=None,
        email_raw=None, source_adapter="nppes",
        source_ref={"npi": "1760646988", "credential": "PSYD",
                    "taxonomies": ["103T00000X"],
                    "practice_city": "NEWARK", "practice_state": "NJ"},
        data_basis=DataBasis.GOVERNMENT_PUBLIC,
    )


class _ConfidentLLM:
    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        assert task == "web_enrich"
        assert "Muhammad Abbass" in prompt and "NEWARK, NJ" in prompt
        return schema.model_validate({
            "identity_confident": True,
            "facts": [
                {"fact_type": "education",
                 "content": "PsyD in Clinical Psychology from Rutgers GSAPP",
                 "source_url": "https://drabbass.example/about"},
                {"fact_type": "specialty",
                 "content": "Specializes in anxiety and OCD in adults",
                 "source_url": "https://drabbass.example/services"},
                {"fact_type": "other", "content": "A fact with no provenance",
                 "source_url": "not-a-url"},
            ],
        })


class _UnsureLLM:
    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        return schema.model_validate({"identity_confident": False, "facts": [
            {"fact_type": "education", "content": "Some other Abbass's degree",
             "source_url": "https://wrong-person.example"}]})


def test_layers_web_facts_over_registry_floor() -> None:
    card = GroundedWebEnricher(_ConfidentLLM()).enrich(_candidate())
    types = [f.fact_type for f in card.facts]
    assert types[0] == "provenance" and "bio" in types        # registry floor
    assert "education" in types and "specialty" in types       # web layer
    edu = next(f for f in card.facts if f.fact_type == "education")
    assert "Rutgers" in edu.content
    assert edu.source_url.startswith("https://drabbass.example")
    # the no-provenance fact was dropped: every fact carries a real source
    assert all(f.source_url.startswith("http") for f in card.facts)


def test_uncertain_identity_keeps_registry_only() -> None:
    card = GroundedWebEnricher(_UnsureLLM()).enrich(_candidate())
    assert {f.fact_type for f in card.facts} == {"provenance", "bio"}
