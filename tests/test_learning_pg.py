"""M3 learning loop against Postgres: trials/successes from the live event
flow, deterministic guardrail pausing, escalation queue, digest."""

from __future__ import annotations

import json
import random

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from tests.conftest import Seed
from tests.test_worker_e2e_pg import StubLLM, make_worker, queue_one

from open_reachout.adapters.fakes import FakeSendingProvider
from open_reachout.core import escalations, events
from open_reachout.core.config import VariantSpec
from open_reachout.core.report import build_report
from open_reachout.stats import persistence

pytestmark = pytest.mark.postgres

VARIANTS = [
    VariantSpec(id="v1", surface="opener", attributes={"tone": "warm"},
                prompt="Write to {{prospect.first_name}} warmly about their work."),
    VariantSpec(id="v2", surface="opener", attributes={"tone": "formal"},
                prompt="Write to {{prospect.first_name}} formally about their work."),
]


def _hook(pg_engine: Engine, provider: FakeSendingProvider, payload: dict) -> None:
    raw = json.dumps(payload).encode()
    with pg_engine.begin() as c:
        events.ingest_webhook(c, provider, raw, provider.sign(raw))


def test_dispatch_records_trial_and_interested_reply_records_success(
    pg_engine: Engine, conn, seed: Seed
) -> None:
    conn.commit()
    touch_id = queue_one(pg_engine, seed)
    provider = FakeSendingProvider()
    make_worker(pg_engine, provider).drain()
    with pg_engine.begin() as c:
        trials, successes = c.execute(
            text("SELECT trials, successes FROM variant_stats WHERE variant_id='v1'")
        ).fetchone()
        assert (trials, successes) == (1, 0)

    _hook(pg_engine, provider, {"id": "r-1", "kind": "reply",
                                "touch_ref": {"touch_id": touch_id},
                                "payload": {"body": "yes, very interested!"}})
    make_worker(pg_engine, provider, StubLLM("interested")).drain()
    with pg_engine.begin() as c:
        successes = c.execute(
            text("SELECT successes FROM variant_stats WHERE variant_id='v1'")
        ).scalar()
        assert successes == 1


def test_bounce_guardrail_pauses_variant_deterministically(
    pg_engine: Engine, conn, seed: Seed
) -> None:
    conn.commit()
    touch_id = queue_one(pg_engine, seed)
    provider = FakeSendingProvider()
    make_worker(pg_engine, provider).drain()
    with pg_engine.begin() as c:
        # 9 prior clean trials; this variant is one bounce from breaching 5%.
        c.execute(text("UPDATE variant_stats SET trials = 10 WHERE variant_id='v1'"))
    _hook(pg_engine, provider, {"id": "b-1", "kind": "bounce",
                                "touch_ref": {"touch_id": touch_id}, "payload": {}})
    with pg_engine.begin() as c:
        paused = c.execute(
            text("SELECT paused FROM variant_stats WHERE variant_id='v1'")
        ).scalar()
        assert paused is True
        audit = c.execute(
            text("SELECT count(*) FROM audit_events WHERE event='guardrail_paused'")
        ).scalar()
        assert audit == 1
        # Selection now refuses the paused arm.
        arms = persistence.load_arms(c, seed.tenant, VARIANTS)
        assert [a.paused for a in arms] == [True, False]
        spec, posterior = persistence.select_variant(c, seed.tenant, VARIANTS,
                                                     random.Random(1))
        assert spec.id == "v2" and posterior["global_rate"] == 0.05


def test_selection_favors_observed_winner(pg_engine: Engine, conn, seed: Seed) -> None:
    conn.commit()
    with pg_engine.begin() as c:
        c.execute(
            text(
                """
                INSERT INTO variant_stats (tenant, variant_id, trials, successes)
                VALUES (:t, 'v1', 200, 30), (:t, 'v2', 200, 4)
                """
            ),
            {"t": seed.tenant},
        )
        rng = random.Random(7)
        picks = [persistence.select_variant(c, seed.tenant, VARIANTS, rng)[0].id
                 for _ in range(100)]
        assert picks.count("v1") > 85


def test_hostile_reply_lands_in_escalation_queue(pg_engine: Engine, conn, seed: Seed) -> None:
    conn.commit()
    touch_id = queue_one(pg_engine, seed)
    provider = FakeSendingProvider()
    make_worker(pg_engine, provider).drain()
    _hook(pg_engine, provider, {"id": "r-2", "kind": "reply",
                                "touch_ref": {"touch_id": touch_id},
                                "payload": {"body": "who gave you my email??"}})
    make_worker(pg_engine, provider, StubLLM("hostile")).drain()
    with pg_engine.begin() as c:
        items = escalations.list_open(c)
        assert len(items) == 1 and items[0].reason == "hostile always escalates"
        item_id = items[0].id
        # System actors cannot resolve (mirrors halt-resume).
        with pytest.raises(PermissionError):
            escalations.resolve(c, item_id, actor="system:agent")
        assert escalations.resolve(c, item_id, actor="operator:cli", note="handled")
        assert escalations.list_open(c) == []


def test_digest_reports_the_live_state(pg_engine: Engine, conn, seed: Seed) -> None:
    conn.commit()
    touch_id = queue_one(pg_engine, seed)
    provider = FakeSendingProvider()
    make_worker(pg_engine, provider).drain()
    _hook(pg_engine, provider, {"id": "r-3", "kind": "reply",
                                "touch_ref": {"touch_id": touch_id},
                                "payload": {"body": "tell me more?"}})
    make_worker(pg_engine, provider, StubLLM("question")).drain()
    with pg_engine.begin() as c:
        digest = build_report(c)
    assert "stagematch: contacted" not in digest  # replied -> engaged
    assert "stagematch: engaged = 1" in digest
    assert "tenant_month stagematch" in digest
    assert "question: 1" in digest
    assert "v1: 0/1 positive" in digest
