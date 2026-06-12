import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

DEFAULT_TEST_DSN = "postgresql+psycopg://orx:orx@127.0.0.1/orx_test"

_TABLES = (
    "decision_traces", "replies", "touches", "evidence_facts", "prospects",
    "entity_keys", "entities", "suppressions", "forget_tombstones",
    "control_flags", "counters", "spend_ledger", "provider_events",
    "audit_events", "jobs", "mailboxes", "config_versions", "tenants",
    "variant_stats", "escalations", "operator_events", "proposals",
)


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    from open_reachout.core.db import apply_schema, engine_from_env

    dsn = os.environ.get("OR_TEST_DSN", DEFAULT_TEST_DSN)
    try:
        engine = engine_from_env(dsn)
        with engine.begin() as conn:
            apply_schema(conn)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no test Postgres available ({exc.__class__.__name__})")
    yield engine
    engine.dispose()


@pytest.fixture
def conn(pg_engine: Engine) -> Iterator[Connection]:
    with pg_engine.begin() as connection:
        connection.execute(
            text("TRUNCATE " + ", ".join(_TABLES) + " RESTART IDENTITY CASCADE")
        )
        yield connection
        # commit on exit: assertions about committed behavior (triggers, etc.)


class Seed:
    """Minimal seeded world for gate tests: one tenant, entity, prospect,
    drafted touch, funded counters, one warmed mailbox."""

    def __init__(self, conn: Connection, *, tenant: str = "stagematch") -> None:
        self.conn = conn
        self.tenant = tenant
        self.month = datetime.now(UTC).strftime("%Y-%m")
        self.tenant_id = str(uuid.uuid4())
        self.entity_id = str(uuid.uuid4())
        self.prospect_id = str(uuid.uuid4())
        self.touch_id = str(uuid.uuid4())
        self.email = "owner@venue.test"
        conn.execute(
            text("INSERT INTO tenants (id, slug) VALUES (CAST(:i AS uuid), :s)"),
            {"i": self.tenant_id, "s": tenant},
        )
        conn.execute(
            text(
                """INSERT INTO entities (id, tenant_id, display_name)
                   VALUES (CAST(:e AS uuid), CAST(:t AS uuid), 'Venue Owner')"""
            ),
            {"e": self.entity_id, "t": self.tenant_id},
        )
        conn.execute(
            text(
                """INSERT INTO prospects (id, tenant_id, entity_id, cohort_id, persona_id,
                       state, email_raw, email_canonical, email_confidence,
                       source_adapter, data_basis)
                   VALUES (CAST(:p AS uuid), CAST(:t AS uuid), CAST(:e AS uuid),
                       'austin_venues', 'small_venue', 'queued', :em, :em, 'verified',
                       'google_places', 'api_terms')"""
            ),
            {"p": self.prospect_id, "t": self.tenant_id, "e": self.entity_id,
             "em": self.email},
        )
        for scope_type, scope_id, cap in (
            ("tenant_month", tenant, 100),
            ("cohort_month", "austin_venues", 50),
        ):
            conn.execute(
                text(
                    """INSERT INTO counters (scope_type, scope_id, period, cap)
                       VALUES (:st, :si, :p, :c)"""
                ),
                {"st": scope_type, "si": scope_id, "p": self.month, "c": cap},
            )
        conn.execute(
            text(
                """INSERT INTO mailboxes (mailbox, tenant, domain, warmup_complete, daily_cap)
                   VALUES ('mb1@try-stagematch.com', :t, 'try-stagematch.com', true, 25)"""
            ),
            {"t": tenant},
        )
        self.new_drafted_touch(self.touch_id)

    def new_drafted_touch(self, touch_id: str, content_hash: str = "h") -> None:
        self.conn.execute(
            text(
                """INSERT INTO touches (id, prospect_id, campaign_id, kind, status,
                       subject, body, content_hash, idempotency_key)
                   VALUES (CAST(:i AS uuid), CAST(:p AS uuid), 'camp-1', 'cold',
                       'drafted', 's', 'b', :h, :i)"""
            ),
            {"i": touch_id, "p": self.prospect_id, "h": content_hash},
        )


@pytest.fixture
def seed(conn: Connection) -> Seed:
    return Seed(conn)
