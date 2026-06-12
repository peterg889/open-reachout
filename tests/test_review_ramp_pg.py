"""Message-review ramp (FR-0.3, spec 8.4): first N drafts per (campaign,
variant) hold for review; approval re-enters the claim path (never bypasses
gates); rejection releases and is recorded as a correction signal.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from tests.conftest import Seed

from open_reachout.core import sendpath
from open_reachout.core.compliance.validators import Draft

pytestmark = pytest.mark.postgres


def _draft(conn: Connection, seed: Seed, n: int, approve_first: int) -> str:
    return sendpath.queue_draft(
        conn, prospect_id=seed.prospect_id, campaign_id="small_venue:opener",
        variant_id="v1", step_index=0, kind="cold",
        draft=Draft(subject=f"s{n}", body=f"b{n}"), content_hash=f"h{n}",
        approve_first=approve_first,
    )


def _status(conn: Connection, touch_id: str) -> str:
    return conn.execute(
        text("SELECT status FROM touches WHERE id = CAST(:i AS uuid)"), {"i": touch_id}
    ).scalar()


def _deliver_jobs(conn: Connection) -> int:
    return conn.execute(
        text("SELECT count(*) FROM jobs WHERE queue = 'deliver' AND payload->>'touch_id' "
             "IN (SELECT id::text FROM touches WHERE campaign_id = 'small_venue:opener')")
    ).scalar()


def test_first_n_hold_then_autopilot(conn: Connection, seed: Seed) -> None:
    first = _draft(conn, seed, 1, approve_first=2)
    second = _draft(conn, seed, 2, approve_first=2)
    third = _draft(conn, seed, 3, approve_first=2)
    assert _status(conn, first) == "pending_review"
    assert _status(conn, second) == "pending_review"
    assert _status(conn, third) == "drafted"          # past N: autopilot
    assert _deliver_jobs(conn) == 1                    # only the third dispatches
    held = sendpath.list_pending_review(conn)
    assert {h[0] for h in held} == {first, second}


def test_approval_reenters_claim_path_human_only(conn: Connection, seed: Seed) -> None:
    held = _draft(conn, seed, 1, approve_first=1)
    with pytest.raises(PermissionError, match="human actor"):
        sendpath.approve_pending(conn, held, actor="system:agent")
    assert sendpath.approve_pending(conn, held, actor="operator:cli")
    assert _status(conn, held) == "drafted"            # claim txn still ahead of it
    assert _deliver_jobs(conn) == 1
    # double-approve is a no-op
    assert sendpath.approve_pending(conn, held, actor="operator:cli") is False


def test_rejection_releases_and_audits(conn: Connection, seed: Seed) -> None:
    held = _draft(conn, seed, 1, approve_first=1)
    assert sendpath.reject_pending(conn, held, actor="operator:cli", note="off voice")
    assert _status(conn, held) == "released"
    assert _deliver_jobs(conn) == 0
    event = conn.execute(
        text("SELECT event FROM audit_events WHERE subject_id = :i "
             "AND event LIKE 'ramp%'"),
        {"i": held},
    ).scalar()
    assert event == "ramp_rejected"


def test_zero_means_no_ramp(conn: Connection, seed: Seed) -> None:
    t = _draft(conn, seed, 1, approve_first=0)
    assert _status(conn, t) == "drafted"
    assert _deliver_jobs(conn) == 1
