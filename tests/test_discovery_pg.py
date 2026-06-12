"""Discovery agent + proposal flow against Postgres (FR-6.1/6.2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from tests.conftest import Seed

from open_reachout.agents import discovery
from open_reachout.core import proposals

pytestmark = pytest.mark.postgres


def _add_cohort_prospects(
    conn: Connection, seed: Seed, cohort: str, *, contacted: int, converted: int
) -> None:
    """Seed `contacted` prospects in `cohort`, of which `converted` converted."""
    period = datetime.now(UTC).strftime("%Y-%m")
    conn.execute(
        text(
            """
            INSERT INTO counters (scope_type, scope_id, period, used, cap)
            VALUES ('cohort_month', :s, :p, 0, 100)
            ON CONFLICT DO NOTHING
            """
        ),
        {"s": cohort, "p": period},
    )
    for i in range(contacted):
        eid, pid = str(uuid.uuid4()), str(uuid.uuid4())
        conn.execute(
            text(
                """INSERT INTO entities (id, tenant_id) VALUES (CAST(:e AS uuid),
                   CAST(:t AS uuid))"""
            ),
            {"e": eid, "t": seed.tenant_id},
        )
        state = "converted" if i < converted else "contacted"
        conn.execute(
            text(
                """INSERT INTO prospects (id, tenant_id, entity_id, cohort_id, persona_id,
                       state, source_adapter, data_basis)
                   VALUES (CAST(:p AS uuid), CAST(:t AS uuid), CAST(:e AS uuid), :c,
                       'x', :st, 'google_places', 'api_terms')"""
            ),
            {"p": pid, "t": seed.tenant_id, "e": eid, "c": cohort, "st": state},
        )


def test_proposes_budget_shift_from_loser_to_winner(conn: Connection, seed: Seed) -> None:
    _add_cohort_prospects(conn, seed, "winners", contacted=40, converted=8)   # 20%
    _add_cohort_prospects(conn, seed, "losers", contacted=40, converted=1)    # 2.5%

    ids = discovery.analyze(conn, seed.tenant)
    assert ids

    open_props = proposals.list_open(conn, seed.tenant)
    shift = next(p for p in open_props if p.kind == "budget_shift")
    assert shift.payload["from_cohort"] == "losers"
    assert shift.payload["to_cohort"] == "winners"
    assert shift.payload["amount"] == 25  # 25% of the loser's cap of 100


def test_insufficient_signal_proposes_nothing(conn: Connection, seed: Seed) -> None:
    _add_cohort_prospects(conn, seed, "tiny_a", contacted=5, converted=1)
    _add_cohort_prospects(conn, seed, "tiny_b", contacted=5, converted=0)
    assert discovery.analyze(conn, seed.tenant) == []


def test_zero_conversion_cohort_flagged_as_opportunity(conn: Connection, seed: Seed) -> None:
    _add_cohort_prospects(conn, seed, "good", contacted=40, converted=6)
    _add_cohort_prospects(conn, seed, "dead", contacted=45, converted=0)
    discovery.analyze(conn, seed.tenant)
    kinds = {p.kind for p in proposals.list_open(conn, seed.tenant)}
    assert "opportunity" in kinds


def test_declined_proposal_not_repitched_within_memory_window(
    conn: Connection, seed: Seed
) -> None:
    _add_cohort_prospects(conn, seed, "winners", contacted=40, converted=8)
    _add_cohort_prospects(conn, seed, "losers", contacted=40, converted=1)
    discovery.analyze(conn, seed.tenant)
    shift = next(p for p in proposals.list_open(conn, seed.tenant) if p.kind == "budget_shift")
    assert proposals.decline(conn, shift.id, actor="operator:cli", note="seasonal")

    # Same analysis again: the declined direction is suppressed.
    discovery.analyze(conn, seed.tenant)
    assert not any(
        p.kind == "budget_shift" for p in proposals.list_open(conn, seed.tenant)
    )


def test_approve_budget_shift_moves_cohort_caps(conn: Connection, seed: Seed) -> None:
    _add_cohort_prospects(conn, seed, "winners", contacted=40, converted=8)
    _add_cohort_prospects(conn, seed, "losers", contacted=40, converted=1)
    discovery.analyze(conn, seed.tenant)
    shift = next(p for p in proposals.list_open(conn, seed.tenant) if p.kind == "budget_shift")

    # System actors cannot approve (mirrors halt-resume / escalations).
    with pytest.raises(PermissionError):
        proposals.approve(conn, shift.id, actor="system:discovery")
    assert proposals.approve(conn, shift.id, actor="operator:cli")

    period = datetime.now(UTC).strftime("%Y-%m")
    caps = dict(
        conn.execute(
            text(
                """
                SELECT scope_id, cap FROM counters
                WHERE scope_type='cohort_month' AND period=:p
                  AND scope_id IN ('winners','losers')
                """
            ),
            {"p": period},
        ).fetchall()
    )
    assert caps == {"winners": 125, "losers": 75}  # +/- 25
    # Re-approval is a no-op (already resolved).
    assert proposals.approve(conn, shift.id, actor="operator:cli") is False


def test_auto_apply_only_for_budget_shift(conn: Connection, seed: Seed) -> None:
    _add_cohort_prospects(conn, seed, "good", contacted=40, converted=6)
    _add_cohort_prospects(conn, seed, "dead", contacted=45, converted=0)
    discovery.analyze(conn, seed.tenant)
    opp = next(p for p in proposals.list_open(conn, seed.tenant) if p.kind == "opportunity")
    # hands_off auto path refuses always-human kinds (FR-0.3).
    with pytest.raises(PermissionError, match="always-human"):
        proposals.approve(conn, opp.id, actor="system:discovery", auto=True)
