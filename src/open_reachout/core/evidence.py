"""Evidence staleness rules (PRD FR-2.5, gate 11).

Facts older than their type's threshold are excluded from personalization —
praising an event series that ended last year is nearly as damaging as
inventing one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from open_reachout.core.interfaces import EvidenceCard, EvidenceFact

#: Per-fact-type staleness thresholds in days (PRD defaults).
STALENESS_DAYS: dict[str, int] = {
    "event_series": 60,
    "calendar": 60,
    "recent_gig": 60,
    "pricing": 90,
    "service": 180,
    "bio": 365,
    "specialty": 365,
}
DEFAULT_STALENESS_DAYS = 90


def is_fresh(fact: EvidenceFact, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    limit = timedelta(days=STALENESS_DAYS.get(fact.fact_type, DEFAULT_STALENESS_DAYS))
    return (now - fact.observed_at) <= limit


def fresh_facts(card: EvidenceCard, now: datetime | None = None) -> list[EvidenceFact]:
    return [f for f in card.facts if is_fresh(f, now)]
