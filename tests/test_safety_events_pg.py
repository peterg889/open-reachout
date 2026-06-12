"""Deterministic safety-event handling (spec 8.5, I-11): complaint and
unsubscribe provider events suppress + transition + feed guardrails with no
LLM anywhere on the path.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from tests.conftest import Seed

from open_reachout.core import events
from open_reachout.core.interfaces import EventKind, ProviderEvent

pytestmark = pytest.mark.postgres


def _event(kind: EventKind, seed: Seed, eid: str) -> ProviderEvent:
    return ProviderEvent(provider_event_id=eid, kind=kind,
                         touch_ref={"touch_id": seed.touch_id})


def _prep(conn: Connection, seed: Seed) -> None:
    conn.execute(
        text("""UPDATE prospects SET state = 'contacted' WHERE id = CAST(:p AS uuid)"""),
        {"p": seed.prospect_id},
    )
    conn.execute(
        text("UPDATE touches SET variant_id = 'v1' WHERE id = CAST(:i AS uuid)"),
        {"i": seed.touch_id},
    )
    conn.execute(
        text("""INSERT INTO variant_stats (tenant, variant_id, trials)
                VALUES (:t, 'v1', 5) ON CONFLICT DO NOTHING"""),
        {"t": seed.tenant},
    )


@pytest.mark.parametrize(
    ("kind", "reason", "state"),
    [
        (EventKind.COMPLAINT, "complaint", "declined"),
        (EventKind.UNSUBSCRIBE, "unsubscribe", "unsubscribed"),
    ],
)
def test_complaint_and_unsubscribe_suppress_deterministically(
    conn: Connection, seed: Seed, kind: EventKind, reason: str, state: str
) -> None:
    _prep(conn, seed)
    processed = events.ingest_events(conn, [_event(kind, seed, f"evt-{reason}")])
    assert processed == 1
    suppressed = conn.execute(
        text("""SELECT reason FROM suppressions WHERE email_canonical = :e"""),
        {"e": seed.email},
    ).scalar()
    assert suppressed == reason
    got_state = conn.execute(
        text("SELECT state FROM prospects WHERE id = CAST(:p AS uuid)"),
        {"p": seed.prospect_id},
    ).scalar()
    assert got_state == state
    guardrail = conn.execute(
        text("""SELECT complaints + unsubs FROM variant_stats
                WHERE tenant = :t AND variant_id = 'v1'"""),
        {"t": seed.tenant},
    ).scalar()
    assert guardrail == 1  # the bandit's guardrail math saw it
    # duplicate delivery: pure no-op (I-10)
    assert events.ingest_events(conn, [_event(kind, seed, f"evt-{reason}")]) == 0
