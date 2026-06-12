"""Event-triggered campaigns (FR-2.9, spec 8.1): operator events fan out via
the trigger queue to `trigger: event` cohorts — starting sequences for
matching qualified prospects and one selector-narrowed discovery pass — while
cadence seeding skips those cohorts entirely. The trigger only *starts*
sequences; every downstream gate applies unchanged.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from tests.conftest import _TABLES

from open_reachout.adapters.fakes import FakeSendingProvider
from open_reachout.api.app import ApiToken, create_app
from open_reachout.core import prospecting
from open_reachout.core.config import TenantConfig, load_tenant
from open_reachout.core.worker import Worker

pytestmark = pytest.mark.postgres

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
EVENT = "license.issued"


@pytest.fixture(autouse=True)
def _clean(pg_engine: Engine):  # noqa: ANN202
    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(_TABLES) + " RESTART IDENTITY CASCADE"))


def _triggered_config() -> TenantConfig:
    """Example tenant plus one event-triggered cohort (budget rebalanced to cap)."""
    raw = load_tenant(EXAMPLES / "music-marketplace" / "tenant.yaml").model_dump()
    cohorts = raw["personas"][0]["cohorts"]
    cohorts[0]["monthly_budget"] = 150
    cohorts.append(
        {
            "id": "new_licensees",
            "filters": {"metro": "austin"},
            "monthly_budget": 50,
            "sources": ["google_places"],
            "trigger": {"event_type": EVENT},
        }
    )
    return TenantConfig.model_validate(raw)


def _qualified_prospect(
    conn: Connection, tenant_id: str, cohort: str, email: str, *, state: str = "qualified"
) -> str:
    entity_id, prospect_id = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute(
        text(
            """INSERT INTO entities (id, tenant_id, display_name)
               VALUES (CAST(:e AS uuid), CAST(:t AS uuid), 'Prospect')"""
        ),
        {"e": entity_id, "t": tenant_id},
    )
    conn.execute(
        text(
            """INSERT INTO prospects (id, tenant_id, entity_id, cohort_id, persona_id,
                   state, email_raw, email_canonical, email_confidence,
                   source_adapter, data_basis)
               VALUES (CAST(:p AS uuid), CAST(:t AS uuid), CAST(:e AS uuid), :c,
                   'small_venue', :st, :em, :em, 'verified', 'fake', 'government_public')"""
        ),
        {"p": prospect_id, "t": tenant_id, "e": entity_id, "c": cohort,
         "em": email, "st": state},
    )
    return prospect_id


def _record_event(
    conn: Connection, *, event_type: str = EVENT,
    selector: dict[str, object] | None = None, dedupe_key: str | None = None,
) -> str:
    row = conn.execute(
        text(
            """INSERT INTO operator_events (event_type, selector, dedupe_key)
               VALUES (:e, CAST(:s AS jsonb), :k) RETURNING id"""
        ),
        {"e": event_type, "s": json.dumps(selector or {}), "k": dedupe_key},
    ).fetchone()
    assert row is not None
    return str(row[0])


def _enqueue_trigger(conn: Connection, event_id: str) -> None:
    from open_reachout.core import queue

    queue.enqueue(conn, "trigger", {"event_id": event_id},
                  idempotency_key=f"trigger:{event_id}")


def _jobs(conn: Connection, queue_name: str) -> list[tuple[dict, str | None]]:
    rows = conn.execute(
        text(
            """SELECT payload, idempotency_key FROM jobs
               WHERE queue = :q AND status = 'ready' ORDER BY id"""
        ),
        {"q": queue_name},
    ).fetchall()
    return [(dict(r[0]), r[1]) for r in rows]


def _drain_triggers(pg_engine: Engine, runtimes: dict) -> None:  # noqa: ANN001
    Worker(pg_engine, handlers={
        "trigger": prospecting.make_trigger_handler(runtimes),
    }).drain()


def test_config_validates_and_seed_discovery_skips_triggered(pg_engine: Engine) -> None:
    cfg = _triggered_config()
    triggered = [c for p in cfg.personas for c in p.cohorts if c.trigger is not None]
    assert [c.id for c in triggered] == ["new_licensees"]
    with pg_engine.begin() as conn:
        runtime = prospecting.runtime_for(conn, cfg)
        n = prospecting.seed_discovery(conn, runtime)
        cadence = sum(1 for p in cfg.personas for c in p.cohorts if c.trigger is None)
        assert n == cadence
        seeded = {j[0]["cohort"] for j in _jobs(conn, "discover")}
        assert "new_licensees" not in seeded


def test_event_starts_sequences_and_scoped_discovery(pg_engine: Engine) -> None:
    cfg = _triggered_config()
    with pg_engine.begin() as conn:
        runtime = prospecting.runtime_for(conn, cfg)
        hit_a = _qualified_prospect(conn, runtime.tenant_id, "new_licensees", "a@lcsw.test")
        hit_b = _qualified_prospect(conn, runtime.tenant_id, "new_licensees", "b@lcsw.test")
        # wrong cohort and wrong state must not match
        _qualified_prospect(conn, runtime.tenant_id, "austin_venues_2026q3", "c@venue.test")
        _qualified_prospect(conn, runtime.tenant_id, "new_licensees", "d@lcsw.test",
                            state="discovered")
        event_id = _record_event(conn, selector={"state": "TX"})
        _enqueue_trigger(conn, event_id)
        runtimes = {cfg.tenant: runtime}
    _drain_triggers(pg_engine, runtimes)
    with pg_engine.begin() as conn:
        compose = _jobs(conn, "compose")
        assert {j[0]["prospect_id"] for j in compose} == {hit_a, hit_b}
        assert all(k == f"trigger:{event_id}:{j['prospect_id']}" for j, k in compose)
        discover = _jobs(conn, "discover")
        assert len(discover) == 1
        payload, key = discover[0]
        assert payload["cohort"] == "new_licensees"
        assert payload["extra_filters"] == {"state": "TX"}
        assert key == f"trigger:{event_id}:discover:new_licensees"


def test_selector_entity_email_narrows_with_alias_canonicalization(
    pg_engine: Engine,
) -> None:
    cfg = _triggered_config()
    with pg_engine.begin() as conn:
        runtime = prospecting.runtime_for(conn, cfg)
        hit = _qualified_prospect(conn, runtime.tenant_id, "new_licensees", "owner@gmail.com")
        _qualified_prospect(conn, runtime.tenant_id, "new_licensees", "other@gmail.com")
        # alias form exercises canonicalization (gate-8 family)
        event_id = _record_event(conn, selector={"entity_email": "Ow.ner+ref@Gmail.com"})
        _enqueue_trigger(conn, event_id)
        runtimes = {cfg.tenant: runtime}
    _drain_triggers(pg_engine, runtimes)
    with pg_engine.begin() as conn:
        assert [j[0]["prospect_id"] for j in _jobs(conn, "compose")] == [hit]


def test_unmatched_event_type_is_a_noop(pg_engine: Engine) -> None:
    cfg = _triggered_config()
    with pg_engine.begin() as conn:
        runtime = prospecting.runtime_for(conn, cfg)
        _qualified_prospect(conn, runtime.tenant_id, "new_licensees", "a@lcsw.test")
        event_id = _record_event(conn, event_type="something.else")
        _enqueue_trigger(conn, event_id)
        runtimes = {cfg.tenant: runtime}
    _drain_triggers(pg_engine, runtimes)
    with pg_engine.begin() as conn:
        assert _jobs(conn, "compose") == []
        assert _jobs(conn, "discover") == []


def test_api_event_enqueues_trigger_exactly_once(pg_engine: Engine) -> None:
    tokens = [ApiToken("ops", "s" * 24, frozenset({"events:write"}))]
    app = create_app(pg_engine, FakeSendingProvider(),
                     attribution_key=b"k" * 16, tokens=tokens)
    from fastapi.testclient import TestClient

    api = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer " + "s" * 24}
    body = {"event_type": EVENT, "selector": {"state": "TX"},
            "payload": {}, "dedupe_key": "evt-1"}
    first = api.post("/v1/events", json=body, headers=headers)
    assert first.status_code == 202 and first.json()["recorded"] is True
    second = api.post("/v1/events", json=body, headers=headers)
    assert second.json()["recorded"] is False
    with pg_engine.begin() as conn:
        triggers = _jobs(conn, "trigger")
        assert len(triggers) == 1
        assert triggers[0][0] == {"event_id": first.json()["id"]}
