"""Postgres-backed job queue (spec section 6).

At-least-once delivery with leases via FOR UPDATE SKIP LOCKED. Handlers must
be idempotent. Tests require a live Postgres (marker: postgres).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

LEASE_SECONDS = 120
MAX_ATTEMPTS_DEFAULT = 5
BACKOFF_BASE_SECONDS = 30


class JobStatus(StrEnum):
    READY = "ready"
    LEASED = "leased"
    DONE = "done"
    DEAD = "dead"


@dataclass(frozen=True)
class Job:
    id: int
    queue: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def enqueue(
    conn: Connection,
    queue: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    run_after_seconds: int = 0,
    max_attempts: int = MAX_ATTEMPTS_DEFAULT,
) -> int | None:
    """Insert a job; returns its id, or None when the idempotency key already
    exists (duplicate submission is a successful no-op)."""
    row = conn.execute(
        text(
            """
            INSERT INTO jobs (queue, payload, idempotency_key, max_attempts, run_after)
            VALUES (:q, CAST(:p AS jsonb), :k, :m, now() + make_interval(secs => :d))
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """
        ),
        {
            "q": queue,
            "p": json.dumps(payload),
            "k": idempotency_key,
            "m": max_attempts,
            "d": run_after_seconds,
        },
    ).fetchone()
    return None if row is None else int(row[0])


def lease(conn: Connection, queue: str) -> Job | None:
    """Lease the next ready job (spec 6: lease, don't lock)."""
    row = conn.execute(
        text(
            """
            UPDATE jobs SET status = 'leased',
                            lease_until = now() + make_interval(secs => :lease),
                            attempts = attempts + 1
            WHERE id = (
                SELECT id FROM jobs
                WHERE queue = :q AND status = 'ready' AND run_after <= now()
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, queue, payload, attempts, max_attempts
            """
        ),
        {"q": queue, "lease": LEASE_SECONDS},
    ).fetchone()
    if row is None:
        return None
    return Job(
        id=int(row[0]), queue=row[1], payload=row[2], attempts=int(row[3]), max_attempts=int(row[4])
    )


def complete(conn: Connection, job_id: int) -> None:
    conn.execute(text("UPDATE jobs SET status='done', lease_until=NULL WHERE id=:i"), {"i": job_id})


def fail(conn: Connection, job: Job, error: str, *, permanent: bool = False) -> None:
    """Retry with exponential backoff, or dead-letter (status 'dead')."""
    if permanent or job.attempts >= job.max_attempts:
        conn.execute(
            text("UPDATE jobs SET status='dead', last_error=:e, lease_until=NULL WHERE id=:i"),
            {"i": job.id, "e": error},
        )
        return
    backoff = BACKOFF_BASE_SECONDS * (2**job.attempts)
    conn.execute(
        text(
            """
            UPDATE jobs SET status='ready', last_error=:e, lease_until=NULL,
                            run_after = now() + make_interval(secs => :b)
            WHERE id=:i
            """
        ),
        {"i": job.id, "e": error, "b": backoff},
    )


def reap_expired(conn: Connection) -> int:
    """Requeue jobs whose lease expired (worker crash). Returns count."""
    result = conn.execute(
        text(
            """
            UPDATE jobs SET status='ready', lease_until=NULL
            WHERE status='leased' AND lease_until < now()
            """
        )
    )
    return result.rowcount or 0
