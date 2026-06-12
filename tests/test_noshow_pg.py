"""No-show handling (FR-4.5, spec 8.14): one delayed re-engagement, then
declined with a 6-month cooldown; the state machine prevents rebooking loops.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from tests.conftest import Seed

from open_reachout.core import dryrun, escalations, noshow
from open_reachout.core.config import TenantConfig, load_tenant
from open_reachout.core.prospecting import runtime_for
from open_reachout.core.worker import Worker

pytestmark = pytest.mark.postgres

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

REENGAGE_PROMPT = (
    "Write a brief, warm note to {{prospect.first_name}} at {{prospect.org_name}} "
    "rescheduling the missed call; reference {{evidence.calendar_highlight}}. "
    "{{persona.voice_rules}}"
)


def _engaged(conn: Connection, seed: Seed) -> None:
    conn.execute(
        text("UPDATE prospects SET persona_id = 'small_venue', state = 'engaged' "
             "WHERE id = CAST(:p AS uuid)"),
        {"p": seed.prospect_id},
    )
    conn.execute(
        text(
            """INSERT INTO evidence_facts (id, prospect_id, fact_type, content,
                   source_url, observed_at)
               VALUES (gen_random_uuid(), CAST(:p AS uuid), 'event_series',
                   CAST('{"summary": "Friday open mic"}' AS jsonb),
                   'https://venue.test/e', now())"""
        ),
        {"p": seed.prospect_id},
    )


def _runtimes(conn: Connection, *, with_prompt: bool) -> dict:
    raw = load_tenant(EXAMPLES / "music-marketplace" / "tenant.yaml").model_dump()
    if with_prompt:
        raw["personas"][0]["reengagement_prompt"] = REENGAGE_PROMPT
    cfg = TenantConfig.model_validate(raw)
    return {cfg.tenant: runtime_for(conn, cfg)}


def test_first_no_show_schedules_single_delayed_reengage(
    conn: Connection, seed: Seed
) -> None:
    _engaged(conn, seed)
    assert noshow.process_no_show(conn, seed.prospect_id) == "reengage_scheduled"
    delayed, run_after_future = conn.execute(
        text(
            """SELECT count(*), bool_and(run_after > now() + interval '1 day')
               FROM jobs WHERE queue = 'reengage'"""
        )
    ).fetchone()
    assert delayed == 1 and run_after_future is True


def test_second_no_show_closes_with_cooldown(conn: Connection, seed: Seed) -> None:
    _engaged(conn, seed)
    noshow.process_no_show(conn, seed.prospect_id)
    assert noshow.process_no_show(conn, seed.prospect_id) == "closed"
    state = conn.execute(
        text("SELECT state FROM prospects WHERE id = CAST(:p AS uuid)"),
        {"p": seed.prospect_id},
    ).scalar()
    assert state == "declined"
    expiry_months = conn.execute(
        text(
            """SELECT round(extract(epoch FROM expires_at - now()) / 2592000)
               FROM suppressions WHERE email_canonical = :e AND reason = 'no_show'"""
        ),
        {"e": seed.email},
    ).scalar()
    assert expiry_months == 6
    # still only the one reengage job — closing never schedules more contact
    n_jobs = conn.execute(
        text("SELECT count(*) FROM jobs WHERE queue = 'reengage'")
    ).scalar()
    assert n_jobs == 1


def test_reengage_without_prompt_escalates(pg_engine, conn, seed: Seed) -> None:  # noqa: ANN001
    _engaged(conn, seed)
    runtimes = _runtimes(conn, with_prompt=False)
    noshow.process_no_show(conn, seed.prospect_id, delay_days=0)
    conn.commit()
    llm = dryrun.ScriptedLLM(
        next(iter(runtimes.values())).validator_ctx, "Maya Reyes, StageMatch"
    )
    Worker(pg_engine, handlers={"reengage": noshow.make_reengage_handler(runtimes, llm)}).drain()
    with pg_engine.begin() as c2:
        (esc,) = [e for e in escalations.list_open(c2) if e.subject_id == seed.prospect_id]
        assert "re-engage manually" in esc.reason


def test_reengage_with_prompt_drafts_one_touch(pg_engine, conn, seed: Seed) -> None:  # noqa: ANN001
    _engaged(conn, seed)
    runtimes = _runtimes(conn, with_prompt=True)
    noshow.process_no_show(conn, seed.prospect_id, delay_days=0)
    conn.commit()
    runtime = next(iter(runtimes.values()))
    llm = dryrun.ScriptedLLM(
        runtime.validator_ctx, runtime.config.brief.about_us.identity.sender
    )
    Worker(pg_engine, handlers={"reengage": noshow.make_reengage_handler(runtimes, llm)}).drain()
    with pg_engine.begin() as c2:
        kind, campaign = c2.execute(
            text(
                """SELECT kind, campaign_id FROM touches
                   WHERE prospect_id = CAST(:p AS uuid) AND campaign_id LIKE '%reengage'"""
            ),
            {"p": seed.prospect_id},
        ).fetchone()
        assert kind == "agentic_reply"
        assert campaign == "small_venue:reengage"


def test_no_show_operator_event_routes_through_trigger(
    pg_engine, conn, seed: Seed  # noqa: ANN001
) -> None:
    """FR-4.5 via FR-2.9: booking.no_show events resolve by entity_email."""
    import json

    from open_reachout.core import prospecting
    from open_reachout.core.queue import enqueue

    _engaged(conn, seed)
    runtimes = _runtimes(conn, with_prompt=False)
    event_id = conn.execute(
        text("""INSERT INTO operator_events (event_type, selector)
                VALUES ('booking.no_show', CAST(:s AS jsonb)) RETURNING id"""),
        {"s": json.dumps({"entity_email": seed.email})},
    ).scalar()
    enqueue(conn, "trigger", {"event_id": str(event_id)},
            idempotency_key=f"trigger:{event_id}")
    conn.commit()
    Worker(pg_engine, handlers={
        "trigger": prospecting.make_trigger_handler(runtimes),
    }).drain()
    with pg_engine.begin() as c:
        n = c.execute(
            text("""SELECT count(*) FROM audit_events
                    WHERE subject_id = :p AND event = 'no_show'"""),
            {"p": seed.prospect_id},
        ).scalar()
        assert n == 1
        reengage = c.execute(
            text("SELECT count(*) FROM jobs WHERE queue = 'reengage'")
        ).scalar()
        assert reengage == 1
