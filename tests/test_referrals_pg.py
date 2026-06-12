"""Referral flow (FR-4.4, spec 8.14): event-gated, once per entity ever,
composed through the full validator path; on-behalf-of drafts are delivered
TO the provider as a human task — never sent as them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from tests.conftest import Seed

from open_reachout.core import dryrun, human_tasks, referrals
from open_reachout.core.config import ReferralSpec, TenantConfig, load_tenant
from open_reachout.core.prospecting import runtime_for

pytestmark = pytest.mark.postgres

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

REFERRAL_PROMPT = (
    "Write a short referral ask to {{prospect.first_name}} at "
    "{{prospect.org_name}}: they just joined; ask if another venue books "
    "live music like {{evidence.calendar_highlight}}. {{persona.voice_rules}}"
)


def _runtimes(conn: Connection, mode: str = "direct") -> dict:
    raw = load_tenant(EXAMPLES / "music-marketplace" / "tenant.yaml").model_dump()
    raw["personas"][0]["referral"] = {"prompt": REFERRAL_PROMPT, "mode": mode}
    cfg = TenantConfig.model_validate(raw)
    return {cfg.tenant: runtime_for(conn, cfg)}


def _make_positive(conn: Connection, seed: Seed, state: str = "converted") -> None:
    conn.execute(
        text("UPDATE prospects SET persona_id = 'small_venue', state = :s "
             "WHERE id = CAST(:p AS uuid)"),
        {"p": seed.prospect_id, "s": state},
    )
    conn.execute(
        text(
            """INSERT INTO evidence_facts (id, prospect_id, fact_type, content,
                   source_url, observed_at)
               VALUES (gen_random_uuid(), CAST(:p AS uuid), 'event_series',
                   CAST('{"summary": "Friday open mic"}' AS jsonb),
                   'https://venue.test/events', now())"""
        ),
        {"p": seed.prospect_id},
    )


def _llm(runtimes: dict):  # noqa: ANN202
    runtime = next(iter(runtimes.values()))
    return dryrun.ScriptedLLM(
        runtime.validator_ctx, runtime.config.brief.about_us.identity.sender
    )


def test_direct_referral_asks_once_per_entity_ever(conn: Connection, seed: Seed) -> None:
    runtimes = _runtimes(conn)
    _make_positive(conn, seed)
    llm = _llm(runtimes)
    touch_id = referrals.process_positive_event(conn, runtimes, llm, seed.prospect_id)
    assert touch_id is not None
    kind, status = conn.execute(
        text("SELECT kind, status FROM touches WHERE id = CAST(:i AS uuid)"),
        {"i": touch_id},
    ).fetchone()
    assert kind == "agentic_reply"   # REPLY gate profile: suppression/halt still bind
    assert status == "drafted"
    asked = conn.execute(
        text("SELECT referral_asked FROM entities WHERE id = CAST(:e AS uuid)"),
        {"e": seed.entity_id},
    ).scalar()
    assert asked is True
    # second positive event for the same entity: never a second ask
    assert referrals.process_positive_event(conn, runtimes, llm, seed.prospect_id) is None


def test_no_referral_config_means_no_ask(conn: Connection, seed: Seed) -> None:
    cfg = load_tenant(EXAMPLES / "music-marketplace" / "tenant.yaml")
    runtimes = {cfg.tenant: runtime_for(conn, cfg)}
    _make_positive(conn, seed)
    assert referrals.process_positive_event(
        conn, runtimes, _llm(runtimes), seed.prospect_id
    ) is None
    asked = conn.execute(
        text("SELECT referral_asked FROM entities WHERE id = CAST(:e AS uuid)"),
        {"e": seed.entity_id},
    ).scalar()
    assert asked is False  # ineligible events must not burn the once-ever flag


def test_neutral_state_prospect_is_ineligible(conn: Connection, seed: Seed) -> None:
    runtimes = _runtimes(conn)
    # seed prospect stays 'queued' — a cold prospect is never asked (FR-4.4)
    conn.execute(
        text("UPDATE prospects SET persona_id = 'small_venue' WHERE id = CAST(:p AS uuid)"),
        {"p": seed.prospect_id},
    )
    assert referrals.process_positive_event(
        conn, runtimes, _llm(runtimes), seed.prospect_id
    ) is None


def test_on_behalf_of_creates_human_task_not_email(conn: Connection, seed: Seed) -> None:
    runtimes = _runtimes(conn, mode="on_behalf_of")
    _make_positive(conn, seed)
    task_id = referrals.process_positive_event(
        conn, runtimes, _llm(runtimes), seed.prospect_id
    )
    assert task_id is not None
    (task,) = human_tasks.list_pending(conn)
    assert task.id == task_id
    assert "THEY send" in task.instruction      # the framework never sends as them
    emails = conn.execute(
        text(
            """SELECT count(*) FROM touches
               WHERE prospect_id = CAST(:p AS uuid) AND kind = 'agentic_reply'"""
        ),
        {"p": seed.prospect_id},
    ).scalar()
    assert emails == 0


def test_referral_spec_validates_prompt_slots() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="unknown variable slot"):
        ReferralSpec(prompt="Use {{made.up_slot}} to write a referral ask now")
