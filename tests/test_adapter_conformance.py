"""Adapter conformance suite (spec §16 contract layer, OSS-5).

Reusable checks every adapter implementation must pass — third-party adapters
import and run these against their own classes. Run here over every built-in.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from open_reachout.adapters.enrich.registry import RegistryEnricher
from open_reachout.adapters.enrich.web_research import GroundedWebEnricher
from open_reachout.adapters.fakes import (
    FakeEnricher,
    FakeFinder,
    FakeSendingProvider,
    FakeSource,
    FakeVerifier,
)
from open_reachout.adapters.sources.nppes import NppesSource
from open_reachout.core.interfaces import Candidate, ConfidenceBucket, DataBasis

FIXTURE = Path(__file__).parent / "fixtures" / "nppes_sample.csv"

CANDIDATE = Candidate(
    display_name="Elena Garcia", org_name="Garcia Therapy", website=None,
    email_raw=None, source_adapter="x",
    source_ref={"npi": "1000000001", "credential": "LMFT",
                "taxonomies": ["106H00000X"],
                "practice_city": "AUSTIN", "practice_state": "TX"},
    data_basis=DataBasis.GOVERNMENT_PUBLIC,
)


class _StubLLM:
    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        return schema.model_validate({"identity_confident": False, "facts": []})


# ---------------------------------------------------------- reusable checks
def assert_source_conformance(source, filters: dict) -> None:  # noqa: ANN001
    """SourceAdapter contract: named, provenance-complete, data_basis-honest."""
    assert isinstance(source.name, str) and source.name
    result = source.discover(filters, None)
    assert result.candidates, "conformance fixture must yield candidates"
    for c in result.candidates:
        assert c.display_name, "every candidate is identifiable"
        assert c.source_adapter, "provenance: source adapter recorded (FR-I.2)"
        assert c.data_basis in set(DataBasis), "data_basis declared honestly"
        assert isinstance(c.source_ref, dict), "adapter-specific provenance payload"


def assert_enricher_conformance(enricher, candidate: Candidate) -> None:  # noqa: ANN001
    """Enricher contract: every fact carries id, source URL, observed_at."""
    card = enricher.enrich(candidate)
    assert card.prospect_ref
    for f in card.facts:
        assert f.fact_id, "claims cite fact_ids (I-4)"
        assert f.source_url, "per-fact provenance (FR-2.5)"
        assert isinstance(f.observed_at, datetime) and f.observed_at.tzinfo, (
            "staleness clock must be tz-aware (gate 11)"
        )


def assert_finder_conformance(finder, candidate: Candidate) -> None:  # noqa: ANN001
    result = finder.find(candidate)
    if result is not None:
        assert "@" in result.email
        assert result.provider, "cost/provenance attribution per waterfall step"


def assert_verifier_conformance(verifier) -> None:  # noqa: ANN001
    result = verifier.verify("someone@example.test")
    assert result.bucket in set(ConfidenceBucket), (
        "calibrated bucket, not a boolean (FR-2.6)"
    )


def assert_sending_conformance(provider) -> None:  # noqa: ANN001
    """SendingProvider contract: typed send + reactive controls exist (I-1/
    spec 7.6). `send` accepts only a ClaimedTouch — unforgeable outside the
    gatekeeper, so we assert the reactive surface and webhook parser here."""
    assert callable(provider.send)
    assert callable(provider.pause_lead)
    assert callable(provider.parse_webhook)


# ----------------------------------------------------------- built-ins pass
def test_nppes_source_conforms() -> None:
    assert_source_conformance(
        NppesSource(FIXTURE), {"state": "TX", "taxonomy": ["106H00000X"]}
    )


def test_fake_source_conforms() -> None:
    assert_source_conformance(FakeSource([CANDIDATE]), {})


@pytest.mark.parametrize(
    "enricher",
    [FakeEnricher(), RegistryEnricher(), GroundedWebEnricher(_StubLLM())],
    ids=["fake", "registry", "web_research"],
)
def test_enrichers_conform(enricher) -> None:  # noqa: ANN001
    assert_enricher_conformance(enricher, CANDIDATE)


def test_finder_and_verifier_conform() -> None:
    assert_finder_conformance(FakeFinder(), CANDIDATE)
    assert_verifier_conformance(FakeVerifier())


def test_sending_providers_conform() -> None:
    assert_sending_conformance(FakeSendingProvider())
    from open_reachout.adapters.sending.smtp import SmtpSendingProvider

    assert_sending_conformance(
        SmtpSendingProvider.__new__(SmtpSendingProvider)  # interface shape only
    )
