"""Program synthesis as a job (PRD FR-9.1, spec 8.8/11.2).

`POST /v1/programs` (or the dashboard's Brief form) validates a Brief and
enqueues a `synthesize` job; the worker — which holds the LLM backend — runs
synthesis and files the result as a Program Proposal (`proposals` row of kind
`program`, exactly like FR-0.6's revision flow). A human approves via the CLI
or management UI; materializing the config artifacts stays an operator-side
`reachout init --from-brief` step (config files live with the operator, not
the worker).
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy.engine import Connection

from open_reachout.agents.synthesizer import SynthesisEscalation, synthesize
from open_reachout.core import proposals, queue
from open_reachout.core.config import Brief
from open_reachout.core.escalations import escalate
from open_reachout.core.interfaces import LLMBackend
from open_reachout.core.queue import Job
from open_reachout.core.worker import Handler


def enqueue_synthesis(conn: Connection, tenant: str, brief: Brief) -> str:
    """Validated Brief -> synthesize job. Idempotent on (tenant, brief hash):
    re-posting the same Brief does not fan out duplicate synthesis."""
    brief_json = json.dumps(brief.model_dump(mode="json"), sort_keys=True)
    digest = hashlib.sha256(brief_json.encode()).hexdigest()[:16]
    queue.enqueue(
        conn, "synthesize", {"tenant": tenant, "brief": json.loads(brief_json)},
        idempotency_key=f"synthesize:{tenant}:{digest}",
    )
    return digest


def make_synthesize_handler(llm: LLMBackend) -> Handler:
    def synthesize_job(conn: Connection, job: Job) -> None:
        tenant = str(job.payload["tenant"])
        brief = Brief.model_validate(job.payload["brief"])
        try:
            program = synthesize(llm, brief, tenant)
        except SynthesisEscalation as exc:
            escalate(conn, tenant=tenant, subject_type="program",
                     subject_id=tenant, reason=str(exc))
            return
        assert program.generated_by is not None
        proposals.propose(
            conn,
            tenant=tenant,
            kind="program",
            summary=(
                f"Synthesized program: {len(program.personas)} persona(s), "
                f"{sum(len(p.cohorts) for p in program.personas)} cohort(s) — "
                "approve, then materialize with `reachout init --from-brief`"
            ),
            payload={
                "personas": [p.model_dump(mode="json") for p in program.personas],
                "brief": brief.model_dump(mode="json"),
                "generated_by": program.generated_by.model_dump(mode="json"),
            },
            dedupe_key=f"program:{tenant}:{program.generated_by.config_hash}",
        )

    return synthesize_job
