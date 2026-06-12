"""Human-task sequence steps (FR-3.6, spec 8.13): the framework briefs, the
operator does the off-channel touch; done counts against frequency caps,
skipped releases, stale tasks expire instead of parking prospects forever.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection
from tests.conftest import Seed

from open_reachout.core import human_tasks
from open_reachout.core.config import SequenceSpec

pytestmark = pytest.mark.postgres


def test_config_validates_human_task_steps() -> None:
    ok = SequenceSpec(steps=3, gaps_days=[4, 7],
                      human_tasks={1: "DM them on Instagram about the Friday slot"})
    assert ok.human_tasks[1].startswith("DM")
    with pytest.raises(ValidationError, match="outside"):
        SequenceSpec(steps=2, gaps_days=[4], human_tasks={5: "walk in on Thursday pls"})
    with pytest.raises(ValidationError, match="too short"):
        SequenceSpec(steps=2, gaps_days=[4], human_tasks={0: "go"})


def _seed_evidence(conn: Connection, seed: Seed) -> None:
    conn.execute(
        text(
            """
            INSERT INTO evidence_facts (id, prospect_id, fact_type, content,
                source_url, observed_at)
            VALUES (gen_random_uuid(), CAST(:p AS uuid), 'event_series',
                CAST(:c AS jsonb), 'https://venue.test/events', now())
            """
        ),
        {"p": seed.prospect_id, "c": '{"summary": "Friday open mic"}'},
    )


def test_create_brief_cites_evidence_and_history(conn: Connection, seed: Seed) -> None:
    _seed_evidence(conn, seed)
    task_id = human_tasks.create_for_step(
        conn, tenant=seed.tenant, prospect_id=seed.prospect_id,
        campaign_id="small_venue:human_task", step_index=0,
        instruction="Walk in Thursday and ask for the booking manager",
        value_prop="free curated local acts",
    )
    (task,) = human_tasks.list_pending(conn, seed.tenant)
    assert task.id == task_id
    assert "Walk in Thursday" in task.brief
    assert "Friday open mic" in task.brief            # evidence cited
    assert "free curated local acts" in task.brief    # value prop present
    status = conn.execute(
        text(
            """SELECT status FROM touches WHERE kind = 'human_task'
               AND prospect_id = CAST(:p AS uuid)"""
        ),
        {"p": seed.prospect_id},
    ).scalar()
    assert status == "pending_human"


def test_done_counts_as_contact_for_frequency_caps(conn: Connection, seed: Seed) -> None:
    task_id = human_tasks.create_for_step(
        conn, tenant=seed.tenant, prospect_id=seed.prospect_id,
        campaign_id="c", step_index=0,
        instruction="Drop by the venue and mention the calendar",
        value_prop="v" * 12,
    )
    before = conn.execute(
        text("SELECT touches_12mo FROM entities WHERE id = CAST(:e AS uuid)"),
        {"e": seed.entity_id},
    ).scalar()
    with pytest.raises(PermissionError, match="human actor"):
        human_tasks.resolve(conn, task_id, actor="system:agent", done=True)
    assert human_tasks.resolve(conn, task_id, actor="operator:cli", done=True,
                               note="met the owner")
    after, last_contact = conn.execute(
        text(
            """SELECT touches_12mo, last_campaign_contact_at FROM entities
               WHERE id = CAST(:e AS uuid)"""
        ),
        {"e": seed.entity_id},
    ).fetchone()
    assert after == before + 1            # I-7: off-channel contact is contact
    assert last_contact is not None
    # double-resolve is a no-op
    assert human_tasks.resolve(conn, task_id, actor="operator:cli", done=True) is False


def test_skip_releases_touch_without_contact(conn: Connection, seed: Seed) -> None:
    task_id = human_tasks.create_for_step(
        conn, tenant=seed.tenant, prospect_id=seed.prospect_id,
        campaign_id="c", step_index=0,
        instruction="Send an Instagram DM about the open slot",
        value_prop="v" * 12,
    )
    before = conn.execute(
        text("SELECT touches_12mo FROM entities WHERE id = CAST(:e AS uuid)"),
        {"e": seed.entity_id},
    ).scalar()
    assert human_tasks.resolve(conn, task_id, actor="operator:cli", done=False)
    after = conn.execute(
        text("SELECT touches_12mo FROM entities WHERE id = CAST(:e AS uuid)"),
        {"e": seed.entity_id},
    ).scalar()
    assert after == before                # skipped is not contact
    status = conn.execute(
        text(
            """SELECT tc.status FROM touches tc JOIN human_tasks ht
               ON ht.touch_id = tc.id WHERE ht.id = CAST(:i AS uuid)"""
        ),
        {"i": task_id},
    ).scalar()
    assert status == "released"


def test_stale_tasks_expire_and_release(conn: Connection, seed: Seed) -> None:
    task_id = human_tasks.create_for_step(
        conn, tenant=seed.tenant, prospect_id=seed.prospect_id,
        campaign_id="c", step_index=0,
        instruction="Call the venue about Friday availability",
        value_prop="v" * 12,
    )
    conn.execute(
        text(
            """UPDATE human_tasks SET created_at = now() - interval '20 days'
               WHERE id = CAST(:i AS uuid)"""
        ),
        {"i": task_id},
    )
    assert human_tasks.expire(conn) == 1
    assert human_tasks.list_pending(conn, seed.tenant) == []
    status = conn.execute(
        text("SELECT status FROM human_tasks WHERE id = CAST(:i AS uuid)"),
        {"i": task_id},
    ).scalar()
    assert status == "expired"
