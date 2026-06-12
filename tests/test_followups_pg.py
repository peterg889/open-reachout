"""Drip follow-up engine (FR-3.5/FR-3.9): a dispatch schedules the next step
after its gap; follow-ups claim under the FOLLOWUP continuation gate; any
reply or exit stops the drip; the final step releases the entity's sequence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from tests.conftest import Seed

from open_reachout.core import sendpath
from open_reachout.core.compliance.validators import Draft, content_hash
from open_reachout.core.config import load_tenant
from open_reachout.core.gatekeeper import ClaimedTouch, GateProfile, Refusal, claim
from open_reachout.core.gatekeeper import DraftTouch as GateDraft
from open_reachout.core.store_pg import PgGateStore

pytestmark = pytest.mark.postgres

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

CTX_KW = dict(
    physical_address="1 Main St, Austin TX",
    unsubscribe_text="reply STOP to opt out",
    sender_identity="Maya Reyes, StageMatch",
    allowed_url_prefixes=("https://ok.test/",),
)
BODY = (
    "Hi - one new thought about your Thursday series. "
    "- Maya Reyes, StageMatch\n1 Main St, Austin TX\nreply STOP to opt out"
)


def _gate_draft(seed: Seed, *, kind: str, step_index: int, touch_id: str) -> GateDraft:
    from open_reachout.core.compliance.validators import ValidatorContext

    draft = Draft(subject="A new angle on Thursdays", body=BODY, step_index=step_index)
    digest = content_hash(draft)
    seed.conn.execute(
        text(
            """UPDATE touches SET content_hash = :h, kind = :k, step_index = :s,
                   subject = :subj, body = :b
               WHERE id = CAST(:i AS uuid)"""
        ),
        {"h": digest, "i": touch_id, "k": kind, "s": step_index,
         "subj": draft.subject, "b": draft.body},
    )
    profile = GateProfile.FOLLOWUP if kind == "followup" else GateProfile.COLD
    return GateDraft(
        touch_id=touch_id, tenant=seed.tenant, entity_id=seed.entity_id,
        email_canonical=seed.email, draft=draft, stored_content_hash=digest,
        groundedness_passed_hash=digest, profile=profile,
        validator_ctx=ValidatorContext(**CTX_KW), cohort_id="austin_venues",
        prospect_id=seed.prospect_id,
    )


def test_followup_claims_only_inside_own_active_sequence(
    conn: Connection, seed: Seed
) -> None:
    # opener claims: opens the sequence
    opener = claim(PgGateStore(conn), _gate_draft(seed, kind="cold", step_index=0,
                                                  touch_id=seed.touch_id))
    assert isinstance(opener, ClaimedTouch)
    # a COLD draft for the same entity is now refused (one active sequence)...
    other = "00000000-0000-4000-8000-00000000c01d"
    seed.new_drafted_touch(other)
    refusal = claim(PgGateStore(conn), _gate_draft(seed, kind="cold", step_index=0,
                                                   touch_id=other))
    assert isinstance(refusal, Refusal) and refusal.reason == "frequency"
    # ...but the FOLLOWUP continuation of the same prospect's sequence claims
    fu = "00000000-0000-4000-8000-0000000000f1"
    seed.new_drafted_touch(fu)
    followup = claim(PgGateStore(conn), _gate_draft(seed, kind="followup",
                                                    step_index=1, touch_id=fu))
    assert isinstance(followup, ClaimedTouch)
    touches_12mo = conn.execute(
        text("SELECT touches_12mo FROM entities WHERE id = CAST(:e AS uuid)"),
        {"e": seed.entity_id},
    ).scalar()
    assert touches_12mo == 2  # each step counts against the annual ceiling


def test_followup_refused_after_sequence_released(conn: Connection, seed: Seed) -> None:
    opener = claim(PgGateStore(conn), _gate_draft(seed, kind="cold", step_index=0,
                                                  touch_id=seed.touch_id))
    assert isinstance(opener, ClaimedTouch)
    assert sendpath.release_sequence(conn, seed.prospect_id)  # reply arrived
    fu = "00000000-0000-4000-8000-0000000000f2"
    seed.new_drafted_touch(fu)
    refusal = claim(PgGateStore(conn), _gate_draft(seed, kind="followup",
                                                   step_index=1, touch_id=fu))
    assert isinstance(refusal, Refusal) and refusal.reason == "frequency"


def test_dispatch_schedules_next_step_then_releases_at_end(
    pg_engine, conn, seed: Seed  # noqa: ANN001
) -> None:
    from open_reachout.adapters.fakes import FakeSendingProvider
    from open_reachout.core.compliance.validators import ValidatorContext
    from open_reachout.core.queue import enqueue
    from open_reachout.core.worker import Worker

    cfg = load_tenant(EXAMPLES / "music-marketplace" / "tenant.yaml")
    sequences = {seed.tenant: {p.id: p.sequence for p in cfg.personas}}
    conn.execute(
        text("UPDATE prospects SET persona_id = 'small_venue', state = 'queued' "
             "WHERE id = CAST(:p AS uuid)"),
        {"p": seed.prospect_id},
    )
    _gate_draft(seed, kind="cold", step_index=0, touch_id=seed.touch_id)  # re-hash
    enqueue(conn, "deliver", {"touch_id": seed.touch_id},
            idempotency_key=f"deliver:{seed.touch_id}")
    conn.commit()
    handler = sendpath.make_deliver_handler(
        FakeSendingProvider(), {seed.tenant: ValidatorContext(**CTX_KW)},
        sequences=sequences,
    )
    Worker(pg_engine, handlers={"deliver": handler}).drain()
    with pg_engine.begin() as c2:
        payload, run_after_future = c2.execute(
            text(
                """SELECT payload, run_after > now() + interval '3 days'
                   FROM jobs WHERE queue = 'compose'
                     AND payload->>'prospect_id' = :p"""
            ),
            {"p": seed.prospect_id},
        ).fetchone()
        assert payload["step_index"] == 1
        assert run_after_future is True  # respects gaps_days[0] = 4
        # final step: dispatching step 2 (of 3) releases the sequence
        fu = "00000000-0000-4000-8000-0000000000f3"
        Seed.new_drafted_touch(_SeedShim(c2, seed), fu)
        _gate_draft(_SeedShim(c2, seed), kind="followup", step_index=2, touch_id=fu)
        enqueue(c2, "deliver", {"touch_id": fu}, idempotency_key=f"deliver:{fu}")
    Worker(pg_engine, handlers={"deliver": handler}).drain()
    with pg_engine.begin() as c3:
        active = c3.execute(
            text("SELECT active_sequence_touch_id FROM entities WHERE id = CAST(:e AS uuid)"),
            {"e": seed.entity_id},
        ).scalar()
        assert active is None  # drip complete: entity unlocked


class _SeedShim:
    """Seed-shaped helper bound to a different connection."""

    def __init__(self, conn: Connection, seed: Seed) -> None:
        self.conn = conn
        self.tenant = seed.tenant
        self.entity_id = seed.entity_id
        self.prospect_id = seed.prospect_id
        self.email = seed.email

    new_drafted_touch = Seed.new_drafted_touch


def test_compose_handler_selects_followup_surface(pg_engine, conn, seed: Seed) -> None:  # noqa: ANN001
    """FR-3.9: a step>0 compose job draws from followup-surface variants and
    queues kind='followup'; the opener surface is never resent."""
    from open_reachout.core import dryrun, prospecting
    from open_reachout.core.queue import enqueue
    from open_reachout.core.worker import Worker

    cfg = load_tenant(EXAMPLES / "music-marketplace" / "tenant.yaml")
    conn.execute(
        text("""UPDATE prospects SET persona_id = 'small_venue', state = 'contacted'
                WHERE id = CAST(:p AS uuid)"""),
        {"p": seed.prospect_id},
    )
    conn.execute(
        text("""INSERT INTO evidence_facts (id, prospect_id, fact_type, content,
                source_url, observed_at)
            VALUES (gen_random_uuid(), CAST(:p AS uuid), 'event_series',
                CAST('{"summary": "Friday open mic"}' AS jsonb),
                'https://venue.test/e', now())"""),
        {"p": seed.prospect_id},
    )
    runtime = prospecting.runtime_for(conn, cfg)
    enqueue(conn, "compose", {"prospect_id": seed.prospect_id, "step_index": 1},
            idempotency_key=f"fu-test:{seed.prospect_id}")
    conn.commit()
    runtimes = {cfg.tenant: runtime}
    llm = dryrun.ScriptedLLM(runtime.validator_ctx,
                             cfg.brief.about_us.identity.sender)
    Worker(pg_engine, handlers={
        "compose": prospecting.make_compose_handler(runtimes, llm),
    }).drain()
    with pg_engine.begin() as c:
        kind, step, variant, campaign = c.execute(
            text("""SELECT kind, step_index, variant_id, campaign_id FROM touches
                    WHERE prospect_id = CAST(:p AS uuid)
                      AND id != CAST(:seed AS uuid)"""),
            {"p": seed.prospect_id, "seed": seed.touch_id},
        ).fetchone()
        assert kind == "followup" and step == 1
        assert variant == "followup_new_angle"          # the followup surface
        assert campaign.endswith("followup_strategy")


def test_compose_handler_releases_when_no_followup_variants(
    pg_engine, conn, seed: Seed  # noqa: ANN001
) -> None:
    from open_reachout.core import dryrun, prospecting
    from open_reachout.core.config import TenantConfig
    from open_reachout.core.queue import enqueue
    from open_reachout.core.worker import Worker

    raw = load_tenant(EXAMPLES / "music-marketplace" / "tenant.yaml").model_dump()
    raw["personas"][0]["variants"] = [
        v for v in raw["personas"][0]["variants"]
        if not v["surface"].startswith("followup")
    ]
    cfg = TenantConfig.model_validate(raw)
    conn.execute(
        text("""UPDATE prospects SET persona_id = 'small_venue', state = 'contacted'
                WHERE id = CAST(:p AS uuid)"""),
        {"p": seed.prospect_id},
    )
    conn.execute(  # an active sequence that should be released
        text("""UPDATE entities SET active_sequence_touch_id = CAST(:t AS uuid)
                WHERE id = CAST(:e AS uuid)"""),
        {"t": seed.touch_id, "e": seed.entity_id},
    )
    runtime = prospecting.runtime_for(conn, cfg)
    enqueue(conn, "compose", {"prospect_id": seed.prospect_id, "step_index": 1},
            idempotency_key=f"fu-none:{seed.prospect_id}")
    conn.commit()
    llm = dryrun.ScriptedLLM(runtime.validator_ctx,
                             cfg.brief.about_us.identity.sender)
    Worker(pg_engine, handlers={
        "compose": prospecting.make_compose_handler({cfg.tenant: runtime}, llm),
    }).drain()
    with pg_engine.begin() as c:
        active = c.execute(
            text("SELECT active_sequence_touch_id FROM entities WHERE id = CAST(:e AS uuid)"),
            {"e": seed.entity_id},
        ).scalar()
        assert active is None  # drip ended cleanly; entity unlocked
