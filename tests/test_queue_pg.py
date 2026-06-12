"""Job queue semantics against real Postgres (spec 6)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core import queue

pytestmark = pytest.mark.postgres


def test_enqueue_lease_complete(conn: Connection) -> None:
    job_id = queue.enqueue(conn, "enrich", {"x": 1})
    assert job_id is not None
    job = queue.lease(conn, "enrich")
    assert job is not None and job.payload == {"x": 1} and job.attempts == 1
    assert queue.lease(conn, "enrich") is None  # leased jobs are invisible
    queue.complete(conn, job.id)
    status = conn.execute(text("SELECT status FROM jobs WHERE id=:i"), {"i": job.id}).scalar()
    assert status == "done"


def test_idempotency_key_dedupes(conn: Connection) -> None:
    first = queue.enqueue(conn, "control", {"op": "pause"}, idempotency_key="k1")
    duplicate = queue.enqueue(conn, "control", {"op": "pause"}, idempotency_key="k1")
    assert first is not None and duplicate is None


def test_retry_backoff_then_dead_letter(conn: Connection) -> None:
    queue.enqueue(conn, "deliver", {"t": "x"}, max_attempts=2)
    job = queue.lease(conn, "deliver")
    assert job is not None
    queue.fail(conn, job, "provider 500")
    # Backed off into the future: not leasable right now.
    assert queue.lease(conn, "deliver") is None
    conn.execute(text("UPDATE jobs SET run_after = now() WHERE id=:i"), {"i": job.id})
    job2 = queue.lease(conn, "deliver")
    assert job2 is not None and job2.attempts == 2
    queue.fail(conn, job2, "provider 500 again")  # attempts == max -> dead
    status, error = conn.execute(
        text("SELECT status, last_error FROM jobs WHERE id=:i"), {"i": job.id}
    ).fetchone()
    assert status == "dead" and "again" in error


def test_permanent_failure_skips_retries(conn: Connection) -> None:
    queue.enqueue(conn, "deliver", {})
    job = queue.lease(conn, "deliver")
    assert job is not None
    queue.fail(conn, job, "validation", permanent=True)
    status = conn.execute(text("SELECT status FROM jobs WHERE id=:i"), {"i": job.id}).scalar()
    assert status == "dead"


def test_reaper_requeues_expired_leases(conn: Connection) -> None:
    queue.enqueue(conn, "classify", {})
    job = queue.lease(conn, "classify")
    assert job is not None
    conn.execute(
        text("UPDATE jobs SET lease_until = now() - interval '1 minute' WHERE id=:i"),
        {"i": job.id},
    )
    assert queue.reap_expired(conn) == 1
    recovered = queue.lease(conn, "classify")
    assert recovered is not None and recovered.id == job.id
