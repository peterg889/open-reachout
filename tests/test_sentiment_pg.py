"""Campaign sentiment auto-throttle (FR-5.6, spec 10.3): EWMA over classified
reply intents per cohort; mildly negative halves the cap once per period,
strongly negative zeroes it and escalates; recovery is operator-only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from tests.conftest import Seed

from open_reachout.core import escalations
from open_reachout.stats import sentiment

pytestmark = pytest.mark.postgres

COHORT = "austin_venues"  # Seed funds this cohort's counter at cap=50


def _reply(conn: Connection, seed: Seed, intent: str) -> None:
    eid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute(
        text("INSERT INTO entities (id, tenant_id) VALUES (CAST(:e AS uuid), CAST(:t AS uuid))"),
        {"e": eid, "t": seed.tenant_id},
    )
    conn.execute(
        text(
            """INSERT INTO prospects (id, tenant_id, entity_id, cohort_id, persona_id,
                   state, source_adapter, data_basis)
               VALUES (CAST(:p AS uuid), CAST(:t AS uuid), CAST(:e AS uuid), :c, 'x',
                   'contacted', 'fake', 'government_public')"""
        ),
        {"p": pid, "t": seed.tenant_id, "e": eid, "c": COHORT},
    )
    conn.execute(
        text(
            """INSERT INTO replies (prospect_id, body, intent, received_at)
               VALUES (CAST(:p AS uuid), 'b', :i, now())"""
        ),
        {"p": pid, "i": intent},
    )


def _cap(conn: Connection) -> int:
    period = datetime.now(UTC).strftime("%Y-%m")
    return conn.execute(
        text(
            """SELECT cap FROM counters WHERE scope_type='cohort_month'
               AND scope_id=:c AND period=:p"""
        ),
        {"c": COHORT, "p": period},
    ).scalar()


def test_positive_mix_takes_no_action(conn: Connection, seed: Seed) -> None:
    for intent in ["interested"] * 8 + ["not_interested"] * 4:
        _reply(conn, seed, intent)
    assert sentiment.evaluate_cohort(conn, seed.tenant, COHORT) is None
    assert _cap(conn) == 50


def test_too_few_replies_never_acts(conn: Connection, seed: Seed) -> None:
    for _ in range(sentiment.MIN_REPLIES - 1):
        _reply(conn, seed, "hostile")
    assert sentiment.evaluate_cohort(conn, seed.tenant, COHORT) is None
    assert _cap(conn) == 50


def test_souring_throttles_once_per_period(conn: Connection, seed: Seed) -> None:
    # EWMA half-life is 20 replies: sustained (not momentary) negativity
    for _ in range(30):
        _reply(conn, seed, "not_interested")
    score, _n = sentiment.cohort_sentiment(conn, seed.tenant, COHORT)
    assert sentiment.PAUSE_BELOW <= score < sentiment.THROTTLE_BELOW
    assert sentiment.evaluate_cohort(conn, seed.tenant, COHORT) == "throttled"
    assert _cap(conn) == 25
    # idempotent within the period: no compounding halvings
    assert sentiment.evaluate_cohort(conn, seed.tenant, COHORT) is None
    assert _cap(conn) == 25


def test_hostile_run_pauses_and_escalates(conn: Connection, seed: Seed) -> None:
    for _ in range(20):
        _reply(conn, seed, "hostile")
    assert sentiment.evaluate_cohort(conn, seed.tenant, COHORT) == "paused"
    assert _cap(conn) == 0
    open_esc = [e for e in escalations.list_open(conn) if e.subject_id == COHORT]
    assert open_esc and "sentiment pause" in open_esc[0].reason
    # the throttle never un-throttles itself: recovery is the operator's move
    assert sentiment.evaluate_cohort(conn, seed.tenant, COHORT) is None
    assert _cap(conn) == 0


def test_classify_path_evaluates_every_nth_reply(conn: Connection, seed: Seed) -> None:
    for _ in range(9):
        _reply(conn, seed, "hostile")
    assert sentiment.maybe_evaluate(conn, seed.tenant, COHORT) is None  # 9th: skip
    _reply(conn, seed, "hostile")
    # 10 hostile replies into a half-life-20 EWMA lands in throttle territory
    assert sentiment.maybe_evaluate(conn, seed.tenant, COHORT) == "throttled"  # 10th
