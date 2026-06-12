"""Full live prospecting pipeline through the worker (spec 8.1-8.4, section 12).

discover -> enrich -> qualify -> compose -> queue_draft -> deliver, with real
entity resolution and persistence. Uses fakes for the account-bound edges.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from tests.conftest import _TABLES

from open_reachout.adapters.fakes import (
    FakeEnricher,
    FakeFinder,
    FakeSendingProvider,
    FakeSource,
    FakeVerifier,
)
from open_reachout.core import dryrun, entity, events, prospecting, sendpath
from open_reachout.core.config import load_tenant
from open_reachout.core.interfaces import Candidate, DataBasis
from open_reachout.core.worker import Worker

pytestmark = pytest.mark.postgres

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture(autouse=True)
def _clean(pg_engine: Engine):  # noqa: ANN202
    # These tests drive the engine directly (not the truncating `conn`
    # fixture), so reset every table up front.
    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(_TABLES) + " RESTART IDENTITY CASCADE"))


def _candidate(name: str, email: str, **ref: object) -> Candidate:
    return Candidate(
        display_name=name, org_name=name, website="https://venue.test", email_raw=email,
        source_adapter="fake", source_ref=ref, data_basis=DataBasis.GOVERNMENT_PUBLIC,
    )


def _full_worker(engine: Engine, runtimes, sources, sending) -> Worker:  # noqa: ANN001
    scripted = dryrun.ScriptedLLM(
        next(iter(runtimes.values())).validator_ctx,
        next(iter(runtimes.values())).config.brief.about_us.identity.sender,
    )
    return Worker(
        engine,
        handlers={
            "discover": prospecting.make_discover_handler(runtimes, sources),
            "enrich": prospecting.make_enrich_handler(
                runtimes, FakeEnricher(), FakeFinder(), FakeVerifier()
            ),
            "qualify": prospecting.make_qualify_handler(runtimes, scripted),
            "compose": prospecting.make_compose_handler(runtimes, scripted),
            "deliver": sendpath.make_deliver_handler(
                sending, {t: r.validator_ctx for t, r in runtimes.items()}
            ),
            "control": events.make_control_handler(sending),
        },
    )


def _runtime(conn):  # noqa: ANN001
    cfg = load_tenant(EXAMPLES / "music-marketplace" / "tenant.yaml")
    return {cfg.tenant: prospecting.runtime_for(conn, cfg)}


def test_discover_to_dispatched_end_to_end(pg_engine: Engine) -> None:
    with pg_engine.begin() as conn:
        runtimes = _runtime(conn)
        runtime = next(iter(runtimes.values()))
        # one warmed mailbox + funded budgets for the tenant
        conn.execute(
            text(
                """INSERT INTO mailboxes (mailbox, tenant, domain, warmup_complete)
                   VALUES ('outreach@get-stagematch.com', :t, 'get-stagematch.com', true)"""
            ),
            {"t": runtime.config.tenant},
        )
        for p in runtime.config.personas:
            for c in p.cohorts:
                conn.execute(
                    text(
                        """INSERT INTO counters (scope_type, scope_id, period, cap)
                           VALUES ('cohort_month', :c, to_char(now(),'YYYY-MM'), 100)"""
                    ),
                    {"c": c.id},
                )
        conn.execute(
            text(
                """INSERT INTO counters (scope_type, scope_id, period, cap)
                   VALUES ('tenant_month', :t, to_char(now(),'YYYY-MM'), 1000)"""
            ),
            {"t": runtime.config.tenant},
        )
        prospecting.seed_discovery(conn, runtime)

    sources = {
        name: FakeSource([_candidate("Cactus Cafe", "booker@venue.test", place_id=name)])
        for name in ("google_places", "indie_on_the_move", "bandsintown", "bandcamp")
    }
    sending = FakeSendingProvider()
    _full_worker(pg_engine, _runtime_reload(pg_engine), sources, sending).drain()

    assert len(sending.sent) >= 1
    with pg_engine.begin() as conn:
        states = dict(
            conn.execute(text("SELECT state, count(*) FROM prospects GROUP BY state")).fetchall()
        )
        assert states.get("contacted", 0) >= 1
        # entity resolution ran: one entity per distinct venue
        assert conn.execute(text("SELECT count(*) FROM entities")).scalar() >= 1


def _runtime_reload(engine: Engine):  # noqa: ANN001
    with engine.begin() as conn:
        return _runtime(conn)


def test_denylisted_and_suppressed_candidates_dropped_at_ingest(pg_engine: Engine) -> None:
    with pg_engine.begin() as conn:
        runtimes = _runtime(conn)
        runtime = next(iter(runtimes.values()))
        persona = runtime.config.personas[0]
        cohort = persona.cohorts[0]

        # Denylisted source: dropped.
        deny = Candidate(
            display_name="X", website="https://www.psychologytoday.com/profile/1",
            email_raw="x@y.test", source_adapter="scrape", data_basis=DataBasis.OWN_SITE_SCRAPE,
        )
        assert prospecting.ingest_candidate(conn, runtime, persona, cohort.id, deny) is None

        # Suppressed address: dropped silently.
        from open_reachout.core import suppression

        suppression.suppress(conn, "blocked@venue.test", reason="unsubscribe")
        sup = _candidate("Y", "blocked@venue.test")
        assert prospecting.ingest_candidate(conn, runtime, persona, cohort.id, sup) is None
        assert conn.execute(text("SELECT count(*) FROM prospects")).scalar() == 0


def test_cross_persona_collision_resolves_to_one_entity(pg_engine: Engine) -> None:
    """A venue owner who also gigs (same email, two personas) is ONE entity —
    the structural basis for cross-campaign frequency caps (FR-2.4, I-7)."""
    with pg_engine.begin() as conn:
        runtime = next(iter(_runtime(conn).values()))
        venue_persona = runtime.config.personas[0]
        band_persona = runtime.config.personas[1]
        same = "maria@example.test"

        prospecting.ingest_candidate(
            conn, runtime, venue_persona, venue_persona.cohorts[0].id,
            _candidate("Maria's Cafe", same),
        )
        prospecting.ingest_candidate(
            conn, runtime, band_persona, band_persona.cohorts[0].id,
            _candidate("Maria's Band", same),
        )
        # Two prospects (one per persona/cohort) but a single shared entity.
        assert conn.execute(text("SELECT count(*) FROM prospects")).scalar() == 2
        assert conn.execute(text("SELECT count(*) FROM entities")).scalar() == 1


def test_entity_resolution_keys() -> None:
    c = Candidate(
        display_name="V", website="https://www.venue.test/", email_raw="A.B+x@Gmail.com",
        phone="(512) 555-0100", source_adapter="x", source_ref={"npi": "123"},
        data_basis=DataBasis.GOVERNMENT_PUBLIC,
    )
    keys = dict(entity.deterministic_keys(c))
    assert keys["email_canonical"] == "ab@gmail.com"  # canonicalized
    assert keys["npi"] == "123"
    assert keys["domain_phone"] == "venue.test|5125550100"  # www stripped, digits only
