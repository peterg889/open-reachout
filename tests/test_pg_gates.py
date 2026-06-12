"""The claim transaction against real Postgres (spec 7.1): the FakeStore unit
tests pinned the semantics; these prove the SQL honors them."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from tests.conftest import Seed

from open_reachout.core import control, forget, suppression
from open_reachout.core.compliance.validators import Draft, ValidatorContext, content_hash
from open_reachout.core.control import ResumeRequiresHumanError
from open_reachout.core.gatekeeper import (
    ClaimedTouch,
    DraftTouch,
    GateProfile,
    Refusal,
    RefusalReason,
    claim,
)
from open_reachout.core.store_pg import PgGateStore

pytestmark = pytest.mark.postgres

CTX = ValidatorContext(
    physical_address="1 Main St, Austin TX",
    unsubscribe_text="reply STOP to opt out",
    sender_identity="Maya Reyes, StageMatch",
    allowed_url_prefixes=("https://ok.test/",),
)
BODY = (
    "Hi - quick note about your venue. "
    "- Maya Reyes, StageMatch\n1 Main St, Austin TX\nreply STOP to opt out"
)


def draft_touch(seed: Seed, touch_id: str | None = None) -> DraftTouch:
    draft = Draft(subject="About Thursday nights", body=BODY)
    digest = content_hash(draft)
    tid = touch_id or seed.touch_id
    seed.conn.execute(
        text("UPDATE touches SET content_hash = :h WHERE id = CAST(:i AS uuid)"),
        {"h": digest, "i": tid},
    )
    return DraftTouch(
        touch_id=tid, tenant=seed.tenant, entity_id=seed.entity_id,
        email_canonical=seed.email, draft=draft, stored_content_hash=digest,
        groundedness_passed_hash=digest, profile=GateProfile.COLD,
        validator_ctx=CTX, cohort_id="austin_venues",
    )


def test_happy_claim_commits_counters_entity_and_trace(conn: Connection, seed: Seed) -> None:
    result = claim(PgGateStore(conn), draft_touch(seed))
    assert isinstance(result, ClaimedTouch)
    assert result.mailbox == "mb1@try-stagematch.com"
    used = dict(
        conn.execute(
            text("SELECT scope_type, used FROM counters WHERE used > 0")
        ).fetchall()
    )
    assert used == {"tenant_month": 1, "cohort_month": 1, "mailbox_day": 1}
    entity = conn.execute(
        text("SELECT touches_12mo, active_sequence_touch_id FROM entities")
    ).fetchone()
    assert entity is not None and entity[0] == 1 and str(entity[1]) == seed.touch_id
    trace = conn.execute(text("SELECT gate_results FROM decision_traces")).fetchone()
    assert trace is not None and trace[0]["suppression"] == "pass"


@pytest.mark.gates
@pytest.mark.disqualifying
def test_gate03_suppression_blocks_and_trigger_backstops(conn: Connection, seed: Seed) -> None:
    suppression.suppress(conn, "Owner+news@Venue.test", reason="unsubscribe")
    result = claim(PgGateStore(conn), draft_touch(seed))
    assert isinstance(result, Refusal) and result.reason is RefusalReason.SUPPRESSED
    # Belt-and-braces: even raw SQL cannot claim a suppressed address (I-3).
    with pytest.raises(Exception, match="invariant I-3"):
        conn.execute(
            text("UPDATE touches SET status='claimed' WHERE id = CAST(:i AS uuid)"),
            {"i": seed.touch_id},
        )


@pytest.mark.gates
@pytest.mark.disqualifying
def test_gate04_halt_blocks_until_human_resume(conn: Connection, seed: Seed) -> None:
    control.halt(conn, actor="system:killswitch")
    result = claim(PgGateStore(conn), draft_touch(seed))
    assert isinstance(result, Refusal) and result.reason is RefusalReason.HALTED
    # The system cannot resume its own kill switch (I-2).
    with pytest.raises(ResumeRequiresHumanError):
        control.resume(conn, actor="system:scheduler")
    assert control.resume(conn, actor="operator:tok-1")
    assert isinstance(claim(PgGateStore(conn), draft_touch(seed)), ClaimedTouch)


@pytest.mark.gates
@pytest.mark.disqualifying
def test_gate05_forget_scrubs_tombstones_and_blocks_rediscovery(
    conn: Connection, seed: Seed
) -> None:
    assert isinstance(claim(PgGateStore(conn), draft_touch(seed)), ClaimedTouch)
    receipt = forget.forget(conn, seed.email)
    assert receipt.addresses_tombstoned == 1

    # PII gone, skeletons scrubbed.
    prospect = conn.execute(
        text("SELECT email_raw, email_canonical, state FROM prospects")
    ).fetchone()
    assert prospect is not None and prospect[0] is None and prospect[1] is None
    assert prospect[2] == "forgotten"
    touch = conn.execute(text("SELECT subject, body, scrubbed FROM touches")).fetchone()
    assert touch is not None and touch[0] is None and touch[2] is True
    assert conn.execute(text("SELECT display_name FROM entities")).fetchone()[0] is None
    # Literal address survives nowhere; only the hash does.
    assert conn.execute(text("SELECT count(*) FROM suppressions")).scalar() == 0
    assert conn.execute(text("SELECT count(*) FROM forget_tombstones")).scalar() == 1
    # Re-discovery is screened out silently — including alias variants.
    assert not suppression.screen_at_ingest(conn, "Owner+x@VENUE.test", seed.tenant)
    # Provider propagation job enqueued with a receipt.
    job = conn.execute(text("SELECT payload FROM jobs WHERE queue='control'")).fetchone()
    assert job is not None and job[0]["op"] == "delete_lead"


def test_gate06_frequency_one_active_sequence_and_annual_cap(
    conn: Connection, seed: Seed
) -> None:
    assert isinstance(claim(PgGateStore(conn), draft_touch(seed)), ClaimedTouch)
    second_id = str(uuid.uuid4())
    seed.new_drafted_touch(second_id)
    result = claim(PgGateStore(conn), draft_touch(seed, second_id))
    assert isinstance(result, Refusal) and result.reason is RefusalReason.FREQUENCY
    # Budget consumed by the refusal attempt was compensated (spec 7.4).
    used = conn.execute(
        text("SELECT used FROM counters WHERE scope_type='tenant_month'")
    ).scalar()
    assert used == 1


def test_gate07_budget_exhaustion_refuses_retryably(conn: Connection, seed: Seed) -> None:
    conn.execute(
        text("UPDATE counters SET cap = 1, used = 1 WHERE scope_type = 'tenant_month'")
    )
    result = claim(PgGateStore(conn), draft_touch(seed))
    assert isinstance(result, Refusal)
    assert result.reason is RefusalReason.BUDGET and not result.terminal


def test_mailbox_capacity_refusal_releases_budget(conn: Connection, seed: Seed) -> None:
    conn.execute(text("UPDATE mailboxes SET warmup_complete = false"))
    result = claim(PgGateStore(conn), draft_touch(seed))
    assert isinstance(result, Refusal) and result.reason is RefusalReason.NO_CAPACITY
    totals = conn.execute(
        text("SELECT coalesce(sum(used),0) FROM counters WHERE scope_type != 'mailbox_day'")
    ).scalar()
    assert totals == 0
    # Compensations are audited, never silent (spec 7.4).
    audits = conn.execute(
        text("SELECT count(*) FROM audit_events WHERE event='adjustment'")
    ).scalar()
    assert audits == 2


def test_unverified_confidence_bucket_is_unsendable(conn: Connection, seed: Seed) -> None:
    conn.execute(text("UPDATE prospects SET email_confidence = 'risky'"))
    result = claim(PgGateStore(conn), draft_touch(seed))
    assert isinstance(result, Refusal) and result.reason is RefusalReason.CONFIDENCE


def test_claim_stamps_registry_version_in_trace(conn: Connection, seed: Seed) -> None:
    """FR-3.2/FR-8.5: every claimed touch records the active claims posture."""
    result = claim(PgGateStore(conn), draft_touch(seed))
    assert isinstance(result, ClaimedTouch)
    version = conn.execute(
        text(
            """SELECT claim_registry_version FROM decision_traces
               WHERE touch_id = CAST(:i AS uuid)"""
        ),
        {"i": seed.touch_id},
    ).scalar()
    assert version == "deny-pack@1"


def test_ensure_registry_records_allowlist_versions(conn: Connection, seed: Seed) -> None:
    from open_reachout.core.compliance.claims import ensure_registry
    from open_reachout.core.config import AboutUs, IdentitySpec

    identity = IdentitySpec(sender="Maya Reyes, StageMatch",
                            physical_address="1 Main St, Austin TX")
    about = AboutUs(name="StageMatch", what_we_do="free venue accounts",
                    identity=identity, claims_mode="allowlist",
                    approved_claims=["free venue accounts", "band membership $9/mo"])
    v1 = ensure_registry(conn, seed.tenant, about)
    assert v1.startswith("allowlist@")
    rows = conn.execute(
        text("SELECT claim_text FROM claim_registry WHERE tenant = :t AND version = :v"),
        {"t": seed.tenant, "v": v1},
    ).fetchall()
    assert {r[0] for r in rows} == {"free venue accounts", "band membership $9/mo"}
    # idempotent re-sync; a changed set is a NEW version, history intact
    assert ensure_registry(conn, seed.tenant, about) == v1
    about2 = about.model_copy(update={"approved_claims": ["free venue accounts"]})
    v2 = ensure_registry(conn, seed.tenant, about2)
    assert v2 != v1
    versions = conn.execute(
        text("SELECT DISTINCT version FROM claim_registry WHERE tenant = :t"),
        {"t": seed.tenant},
    ).fetchall()
    assert len(versions) == 2
