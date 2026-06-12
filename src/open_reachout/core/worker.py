"""Queue worker (spec 6): lease in one transaction, handle in another,
complete/fail in a third. At-least-once with idempotent handlers.

Error taxonomy (spec 6): RetryableJobError (default for unexpected
exceptions) backs off; PermanentJobError dead-letters immediately;
ComplianceJobError dead-letters AND writes an alert audit row — these
indicate pressure on an invariant and a human should look.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from open_reachout.core import queue
from open_reachout.core.queue import Job

#: Control first (suppression/halt propagation is never queued behind work),
#: then inbound events, then the outbound send, then the prospecting build-up
#: with later stages ahead of earlier ones so in-flight prospects finish
#: before new discovery floods the queues.
QUEUE_PRIORITY = (
    "control", "classify", "deliver", "compose", "qualify", "enrich", "discover",
)

Handler = Callable[[Connection, Job], None]


class PermanentJobError(Exception):
    pass


class ComplianceJobError(Exception):
    pass


class Worker:
    def __init__(self, engine: Engine, handlers: dict[str, Handler]) -> None:
        self.engine = engine
        self.handlers = handlers

    def run_once(self) -> bool:
        """Process at most one job across queues in priority order.
        Returns False when every queue is empty."""
        for queue_name in QUEUE_PRIORITY:
            if queue_name not in self.handlers:
                continue
            with self.engine.begin() as conn:
                job = queue.lease(conn, queue_name)
            if job is None:
                continue
            self._handle(job)
            return True
        return False

    def drain(self, *, max_jobs: int = 1000) -> int:
        """Process until idle (tests, `reachout run --once`)."""
        with self.engine.begin() as conn:
            queue.reap_expired(conn)
        processed = 0
        while processed < max_jobs and self.run_once():
            processed += 1
        return processed

    def run_forever(self, *, poll_seconds: float = 2.0, reap_every: int = 30) -> None:
        ticks = 0
        while True:  # pragma: no cover - exercised via drain() in tests
            if not self.run_once():
                time.sleep(poll_seconds)
            ticks += 1
            if ticks % reap_every == 0:
                with self.engine.begin() as conn:
                    queue.reap_expired(conn)

    def _handle(self, job: Job) -> None:
        try:
            with self.engine.begin() as conn:
                self.handlers[job.queue](conn, job)
        except ComplianceJobError as exc:
            with self.engine.begin() as conn:
                queue.fail(conn, job, f"compliance: {exc}", permanent=True)
                conn.execute(
                    text(
                        """
                        INSERT INTO audit_events (subject_type, subject_id, event,
                            payload, actor)
                        VALUES ('job', :i, 'compliance_alert',
                            CAST(:pl AS jsonb), 'system:worker')
                        """
                    ),
                    {"i": str(job.id), "pl": f'{{"error": "{exc}"}}'},
                )
        except PermanentJobError as exc:
            with self.engine.begin() as conn:
                queue.fail(conn, job, str(exc), permanent=True)
        except Exception as exc:  # noqa: BLE001 — retryable by default
            with self.engine.begin() as conn:
                queue.fail(conn, job, repr(exc))
        else:
            with self.engine.begin() as conn:
                queue.complete(conn, job.id)
