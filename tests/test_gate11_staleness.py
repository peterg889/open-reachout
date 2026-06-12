"""Gate 11 (PRD §10, FR-2.5): facts past their per-type staleness threshold
are excluded from composition. Praising an event series that ended last year
is nearly as damaging as inventing one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from open_reachout.core.evidence import STALENESS_DAYS, is_fresh
from open_reachout.core.interfaces import EvidenceCard, EvidenceFact

pytestmark = pytest.mark.gates

NOW = datetime(2026, 6, 12, tzinfo=UTC)


def _fact(fact_type: str, age_days: int) -> EvidenceFact:
    return EvidenceFact(
        fact_id=f"{fact_type}-{age_days}", fact_type=fact_type,
        content=f"a {fact_type} fact", source_url="https://x.test",
        observed_at=NOW - timedelta(days=age_days),
    )


@pytest.mark.parametrize(
    ("fact_type", "threshold"),
    [("event_series", 60), ("calendar", 60), ("pricing", 90), ("bio", 365)],
)
def test_gate11_per_type_thresholds(fact_type: str, threshold: int) -> None:
    assert STALENESS_DAYS[fact_type] == threshold
    assert is_fresh(_fact(fact_type, threshold - 1), NOW)
    assert not is_fresh(_fact(fact_type, threshold + 1), NOW)


def test_gate11_unknown_types_get_conservative_default() -> None:
    assert is_fresh(_fact("never_seen_type", 89), NOW)
    assert not is_fresh(_fact("never_seen_type", 91), NOW)


def test_gate11_stale_facts_never_reach_composition() -> None:
    """The composition substrate is fresh_facts; a card whose only facts are
    stale yields nothing to personalize with — resolution fails closed."""
    from pathlib import Path

    from open_reachout.core.config import load_tenant
    from open_reachout.core.dryrun import build_values
    from open_reachout.core.interfaces import Candidate, DataBasis
    from open_reachout.core.variables import resolve

    cfg = load_tenant(
        Path(__file__).resolve().parents[1]
        / "examples" / "music-marketplace" / "tenant.yaml"
    )
    persona = cfg.personas[0]
    prompt = persona.variants[0].prompt  # references {{evidence.*}}
    candidate = Candidate(display_name="Sam Venue", org_name="Cactus Cafe",
                          email_raw="s@v.test", source_adapter="fake",
                          source_ref={}, data_basis=DataBasis.GOVERNMENT_PUBLIC)
    stale_card = EvidenceCard(prospect_ref="x", facts=[_fact("event_series", 75)])
    values = build_values(prompt, candidate, stale_card, cfg, persona)
    with pytest.raises(KeyError):
        resolve(prompt, values)  # stale-only evidence: escalate, never compose

    fresh_card = EvidenceCard(prospect_ref="x", facts=[_fact("event_series", 5)])
    values = build_values(prompt, candidate, fresh_card, cfg, persona)
    resolved = resolve(prompt, values)
    assert resolved is not None  # fresh evidence composes
