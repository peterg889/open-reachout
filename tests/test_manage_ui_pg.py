"""Management surface (FR-9.1/9.4): mutations go through the same service
layer as the CLI; the manage token gates everything; rebalancing flags get
one-click approve/decline.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from tests.conftest import Seed

from open_reachout.adapters.fakes import FakeSendingProvider
from open_reachout.api.app import ApiToken, create_app
from open_reachout.core import proposals, sendpath
from open_reachout.core.compliance.validators import Draft

pytestmark = pytest.mark.postgres

TOKENS = [ApiToken("ops", "s" * 24, frozenset({"read"}))]


def _client(pg_engine: Engine, monkeypatch, manage_token: str | None) -> TestClient:  # noqa: ANN001
    if manage_token is None:
        monkeypatch.delenv("OR_DASHBOARD_MANAGE_TOKEN", raising=False)
    else:
        monkeypatch.setenv("OR_DASHBOARD_MANAGE_TOKEN", manage_token)
    app = create_app(pg_engine, FakeSendingProvider(),
                     attribution_key=b"k" * 16, tokens=TOKENS)
    return TestClient(app, raise_server_exceptions=False)


def _rebalance_proposal(conn: Connection, seed: Seed) -> str:
    pid = proposals.propose(
        conn, tenant=seed.tenant, kind="rebalance",
        summary="Rebalance austin_venues: conversion upper bound 2% < floor 10%",
        payload={"action": "pause", "cohort": "austin_venues"},
        dedupe_key="rebalance:austin_venues",
    )
    assert pid
    return pid


def test_disabled_without_manage_token(pg_engine, conn, seed, monkeypatch) -> None:  # noqa: ANN001
    api = _client(pg_engine, monkeypatch, manage_token=None)
    assert api.get("/dashboard/queues").status_code == 403


def test_wrong_token_unauthorized(pg_engine, conn, seed, monkeypatch) -> None:  # noqa: ANN001
    api = _client(pg_engine, monkeypatch, manage_token="m" * 24)
    assert api.get("/dashboard/queues?manage_token=nope").status_code == 401
    pid = _rebalance_proposal(conn, seed)
    conn.commit()
    assert api.post(f"/dashboard/proposals/{pid}/approve?manage_token=no").status_code == 401


def test_rebalance_flag_one_click_approve_applies(
    pg_engine, conn, seed, monkeypatch  # noqa: ANN001
) -> None:
    from datetime import UTC, datetime

    pid = _rebalance_proposal(conn, seed)
    conn.commit()
    api = _client(pg_engine, monkeypatch, manage_token="m" * 24)
    page = api.get("/dashboard/queues?manage_token=" + "m" * 24)
    assert page.status_code == 200
    assert "rebalancing flag" in page.text          # FR-9.4: flagged inline
    resp = api.post(f"/dashboard/proposals/{pid}/approve?manage_token=" + "m" * 24,
                    follow_redirects=False)
    assert resp.status_code == 303
    period = datetime.now(UTC).strftime("%Y-%m")
    with pg_engine.begin() as c:
        cap = c.execute(
            text("""SELECT cap FROM counters WHERE scope_type='cohort_month'
                    AND scope_id='austin_venues' AND period=:p"""),
            {"p": period},
        ).scalar()
        assert cap == 0                              # pause applied via service layer
        actor = c.execute(
            text("SELECT resolved_by FROM proposals WHERE id = CAST(:i AS uuid)"),
            {"i": pid},
        ).scalar()
        assert actor == "operator:dashboard"


def test_ramp_send_reenters_claim_path(pg_engine, conn, seed, monkeypatch) -> None:  # noqa: ANN001
    held = sendpath.queue_draft(
        conn, prospect_id=seed.prospect_id, campaign_id="c", variant_id="v",
        step_index=0, kind="cold", draft=Draft(subject="s", body="b"),
        content_hash="h", approve_first=1,
    )
    conn.commit()
    api = _client(pg_engine, monkeypatch, manage_token="m" * 24)
    resp = api.post(f"/dashboard/touches/{held}/send?manage_token=" + "m" * 24,
                    follow_redirects=False)
    assert resp.status_code == 303
    with pg_engine.begin() as c:
        status = c.execute(
            text("SELECT status FROM touches WHERE id = CAST(:i AS uuid)"), {"i": held}
        ).scalar()
        assert status == "drafted"                   # gatekeeper still ahead of it


def test_new_campaign_onboarding_flow(pg_engine, conn, seed, monkeypatch) -> None:  # noqa: ANN001
    """FR-9.1: Brief form -> synthesis job -> proposal queue (nothing sends)."""
    conn.commit()
    api = _client(pg_engine, monkeypatch, manage_token="m" * 24)
    form_page = api.get("/dashboard/new-campaign?manage_token=" + "m" * 24)
    assert form_page.status_code == 200
    assert "Who should we find?" in form_page.text
    good = {
        "tenant": "nj-providers",
        "find": "Licensed therapists in private practice across New Jersey",
        "research": "Read their site for license type and new-client status",
        "convert": "provider claims their verified profile",
        "name": "TheraDirectory",
        "what_we_do": "A verified therapist directory free until first inquiry",
        "sender": "Dana Whitfield, TheraDirectory",
        "physical_address": "210 Market St Suite 4, Camden NJ 08102",
        "signup_link": "https://theradirectory.test/claim",
        "monthly_prospects": "150",
    }
    resp = api.post("/dashboard/campaigns/new?manage_token=" + "m" * 24, data=good)
    assert resp.status_code == 200 and "Synthesis queued" in resp.text
    with pg_engine.begin() as c:
        n = c.execute(text("SELECT count(*) FROM jobs WHERE queue='synthesize'")).scalar()
        assert n == 1
    # invalid brief: friendly validation feedback, no job
    bad = dict(good, find="too short", tenant="bad2")
    resp = api.post("/dashboard/campaigns/new?manage_token=" + "m" * 24, data=bad)
    assert "needs a fix" in resp.text
    with pg_engine.begin() as c:
        n = c.execute(text("SELECT count(*) FROM jobs WHERE queue='synthesize'")).scalar()
        assert n == 1


def test_task_and_ramp_and_control_routes(pg_engine, conn, seed, monkeypatch) -> None:  # noqa: ANN001
    """Round out the management surface: task done/skip, ramp reject,
    tenant pause/resume — all through the service layer with audit."""
    from open_reachout.core import human_tasks, sendpath
    from open_reachout.core.compliance.validators import Draft

    task = human_tasks.create_for_step(
        conn, tenant=seed.tenant, prospect_id=seed.prospect_id,
        campaign_id="c", step_index=0,
        instruction="Walk in Thursday and ask for the booking manager",
        value_prop="free curated local acts",
    )
    held = sendpath.queue_draft(
        conn, prospect_id=seed.prospect_id, campaign_id="c2", variant_id="v",
        step_index=0, kind="cold", draft=Draft(subject="s", body="b"),
        content_hash="h2", approve_first=1,
    )
    conn.commit()
    api = _client(pg_engine, monkeypatch, manage_token="m" * 24)
    q = "?manage_token=" + "m" * 24

    assert api.post(f"/dashboard/touches/{held}/reject{q}",
                    follow_redirects=False).status_code == 303
    assert api.post(f"/dashboard/tasks/{task}/done{q}",
                    follow_redirects=False).status_code == 303
    assert api.post(f"/dashboard/tasks/{task}/skip{q}",
                    follow_redirects=False).status_code == 404  # already resolved
    assert api.post(f"/dashboard/tenants/{seed.tenant}/pause{q}",
                    follow_redirects=False).status_code == 303
    with pg_engine.begin() as c:
        halted = c.execute(text("SELECT count(*) FROM control_flags WHERE scope = :s"),
                           {"s": seed.tenant}).scalar()
        assert halted == 1
    assert api.post(f"/dashboard/tenants/{seed.tenant}/resume{q}",
                    follow_redirects=False).status_code == 303
    with pg_engine.begin() as c:
        halted = c.execute(text("SELECT count(*) FROM control_flags WHERE scope = :s"),
                           {"s": seed.tenant}).scalar()
        assert halted == 0
