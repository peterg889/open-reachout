"""Concurrency races (spec §16): two workers / one budget slot; the ramp's
Nth-draft race; suppress-between-claim-and-dispatch reactive enforcement.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from tests.conftest import Seed

from open_reachout.core import sendpath, suppression
from open_reachout.core.compliance.validators import Draft, ValidatorContext, content_hash
from open_reachout.core.gatekeeper import (
    ClaimedTouch,
    DraftTouch,
    GateProfile,
    Refusal,
    claim,
    reissue,
)
from open_reachout.core.store_pg import PgGateStore

pytestmark = pytest.mark.postgres

CTX = ValidatorContext(
    physical_address="1 Main St, Austin TX",
    unsubscribe_text="reply STOP to opt out",
    sender_identity="Maya Reyes, StageMatch",
    allowed_url_prefixes=("https://ok.test/",),
)
BODY = ("Hi - quick note about your venue. "
        "- Maya Reyes, StageMatch\n1 Main St, Austin TX\nreply STOP to opt out")


def _second_prospect(conn, seed: Seed) -> tuple[str, str, str]:  # noqa: ANN001
    """A second entity+prospect+drafted touch (frequency caps are per entity,
    so a budget race needs two distinct entities)."""
    eid, pid, tid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute(
        text("INSERT INTO entities (id, tenant_id) VALUES (CAST(:e AS uuid), "
             "CAST(:t AS uuid))"),
        {"e": eid, "t": seed.tenant_id},
    )
    conn.execute(
        text("""INSERT INTO prospects (id, tenant_id, entity_id, cohort_id, persona_id,
                state, email_raw, email_canonical, email_confidence, source_adapter,
                data_basis)
            VALUES (CAST(:p AS uuid), CAST(:t AS uuid), CAST(:e AS uuid),
                'austin_venues', 'x', 'queued', 'two@venue.test', 'two@venue.test',
                'verified', 'fake', 'government_public')"""),
        {"p": pid, "t": seed.tenant_id, "e": eid},
    )
    draft = Draft(subject="About Thursday nights", body=BODY)
    conn.execute(
        text("""INSERT INTO touches (id, prospect_id, campaign_id, kind, status,
                subject, body, content_hash, idempotency_key)
            VALUES (CAST(:i AS uuid), CAST(:p AS uuid), 'camp-1', 'cold', 'drafted',
                :s, :b, :h, :i)"""),
        {"i": tid, "p": pid, "s": draft.subject, "b": draft.body,
         "h": content_hash(draft)},
    )
    return eid, pid, tid


def _draft_for(seed_like, touch_id: str, entity_id: str, email: str) -> DraftTouch:  # noqa: ANN001
    draft = Draft(subject="About Thursday nights", body=BODY)
    return DraftTouch(
        touch_id=touch_id, tenant="stagematch", entity_id=entity_id,
        email_canonical=email, draft=draft, stored_content_hash=content_hash(draft),
        groundedness_passed_hash=content_hash(draft), profile=GateProfile.COLD,
        validator_ctx=CTX, cohort_id="austin_venues",
    )


def test_two_workers_one_budget_slot(pg_engine: Engine, conn, seed: Seed) -> None:  # noqa: ANN001
    """Spec §16: exactly one of two concurrent claims wins the last slot."""
    period = datetime.now(UTC).strftime("%Y-%m")
    eid2, _pid2, tid2 = _second_prospect(conn, seed)
    conn.execute(  # update both touches' hashes to the real body
        text("UPDATE touches SET content_hash = :h, subject = :s, body = :b"),
        {"h": content_hash(Draft(subject="About Thursday nights", body=BODY)),
         "s": "About Thursday nights", "b": BODY},
    )
    conn.execute(
        text("""UPDATE counters SET cap = used + 1
                WHERE scope_type = 'cohort_month' AND scope_id = 'austin_venues'
                  AND period = :p"""),
        {"p": period},
    )
    conn.commit()

    drafts = [
        _draft_for(seed, seed.touch_id, seed.entity_id, seed.email),
        _draft_for(seed, tid2, eid2, "two@venue.test"),
    ]

    def attempt(draft: DraftTouch):  # noqa: ANN202
        with pg_engine.begin() as c:
            return claim(PgGateStore(c), draft)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, drafts))

    wins = [r for r in results if isinstance(r, ClaimedTouch)]
    refusals = [r for r in results if isinstance(r, Refusal)]
    assert len(wins) == 1, f"exactly one claim wins the slot, got {results}"
    assert len(refusals) == 1 and refusals[0].reason == "budget"


def test_ramp_nth_draft_race_holds_exactly_n(pg_engine: Engine, conn, seed: Seed) -> None:  # noqa: ANN001
    """FR-0.3: two concurrent drafts cannot both count as 'the first'."""
    conn.commit()
    draft = Draft(subject="s", body="b")

    def queue_one(n: int) -> str:
        with pg_engine.begin() as c:
            return sendpath.queue_draft(
                c, prospect_id=seed.prospect_id, campaign_id="ramp-race",
                variant_id="v1", step_index=0, kind="cold", draft=draft,
                content_hash=f"h{n}", approve_first=1,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(queue_one, [1, 2]))

    with pg_engine.begin() as c:
        statuses = sorted(
            r[0] for r in c.execute(
                text("SELECT status FROM touches WHERE id::text = ANY(:ids)"),
                {"ids": ids},
            )
        )
    assert statuses == ["drafted", "pending_review"], statuses


def test_suppress_between_claim_and_dispatch_blocks_reissue(
    conn, seed: Seed  # noqa: ANN001
) -> None:
    """Spec §16 'suppress-during-dispatch': the dispatch-retry path re-checks
    the absolute gates; a suppression landing after claim blocks the send."""
    result = claim(PgGateStore(conn),
                   _draft_for(seed, seed.touch_id, seed.entity_id, seed.email))
    assert isinstance(result, ClaimedTouch)
    # dispatch retry works while the address is clean...
    again = reissue(PgGateStore(conn), seed.touch_id)
    assert isinstance(again, ClaimedTouch)
    # ...then the prospect opts out between claim and the retry
    suppression.suppress(conn, seed.email, reason="unsubscribe")
    blocked = reissue(PgGateStore(conn), seed.touch_id)
    assert isinstance(blocked, Refusal) and blocked.terminal
