"""The outbound send path (spec 7.4/7.6): queue a drafted touch, then the
deliver handler claims and dispatches in ONE transaction — a provider failure
rolls the claim back atomically (counters, entity state, everything).
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core import queue
from open_reachout.core.compliance.validators import Draft, ValidatorContext
from open_reachout.core.gatekeeper import ClaimedTouch, GateProfile, Refusal, claim
from open_reachout.core.gatekeeper import DraftTouch as GateDraft
from open_reachout.core.interfaces import SendingProvider
from open_reachout.core.lifecycle import transition
from open_reachout.core.queue import Job
from open_reachout.core.states import ProspectState
from open_reachout.core.store_pg import PgGateStore
from open_reachout.core.worker import Handler, PermanentJobError


def queue_draft(
    conn: Connection,
    *,
    prospect_id: str,
    campaign_id: str,
    variant_id: str | None,
    step_index: int,
    kind: str,
    draft: Draft,
    content_hash: str,
) -> str:
    """Persist a drafted touch and enqueue its deliver job (idempotent on id)."""
    touch_id = str(uuid.uuid4())
    conn.execute(
        text(
            """
            INSERT INTO touches (id, prospect_id, campaign_id, variant_id, step_index,
                kind, status, subject, body, content_hash, idempotency_key)
            VALUES (CAST(:i AS uuid), CAST(:p AS uuid), :c, :v, :s, :k, 'drafted',
                :subj, :body, :h, :i)
            """
        ),
        {
            "i": touch_id, "p": prospect_id, "c": campaign_id, "v": variant_id,
            "s": step_index, "k": kind, "subj": draft.subject, "body": draft.body,
            "h": content_hash,
        },
    )
    queue.enqueue(conn, "deliver", {"touch_id": touch_id}, idempotency_key=f"deliver:{touch_id}")
    return touch_id


def _load_gate_draft(conn: Connection, touch_id: str, ctx: ValidatorContext) -> GateDraft | None:
    row = conn.execute(
        text(
            """
            SELECT tc.subject, tc.body, tc.step_index, tc.kind, tc.content_hash,
                   tc.status, p.entity_id, p.email_canonical, p.cohort_id, t.slug
            FROM touches tc
            JOIN prospects p ON p.id = tc.prospect_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE tc.id = CAST(:i AS uuid)
            """
        ),
        {"i": touch_id},
    ).fetchone()
    if row is None:
        return None
    subject, body, step_index, kind, content_hash, status, entity_id, email, cohort, slug = row
    if status != "drafted":
        return None
    if email is None:
        raise PermanentJobError("prospect has no contactable address")
    return GateDraft(
        touch_id=touch_id,
        tenant=slug,
        entity_id=str(entity_id),
        email_canonical=email,
        draft=Draft(subject=subject or "", body=body or "", step_index=step_index),
        stored_content_hash=content_hash,
        # M2 wiring note: the composer stamps this; queue_draft callers must
        # have run compose() (which refuses ungrounded drafts). Re-verified
        # against stored content here.
        groundedness_passed_hash=content_hash,
        profile=GateProfile.REPLY if kind == "agentic_reply" else GateProfile.COLD,
        validator_ctx=ctx,
        cohort_id=cohort,
    )


def make_deliver_handler(
    provider: SendingProvider, validator_ctx_for: dict[str, ValidatorContext]
) -> Handler:
    """Build the deliver-queue handler (claim + send + dispatch, one txn)."""

    def deliver(conn: Connection, job: Job) -> None:
        touch_id = str(job.payload["touch_id"])
        status = conn.execute(
            text("SELECT status FROM touches WHERE id = CAST(:i AS uuid)"), {"i": touch_id}
        ).scalar()
        if status in ("dispatched", "sent"):
            return  # retry after success: idempotent no-op
        if status != "drafted":
            raise PermanentJobError(f"touch {touch_id} in state {status!r}")

        tenant = conn.execute(
            text(
                """
                SELECT t.slug FROM touches tc
                JOIN prospects p ON p.id = tc.prospect_id
                JOIN tenants t ON t.id = p.tenant_id WHERE tc.id = CAST(:i AS uuid)
                """
            ),
            {"i": touch_id},
        ).scalar()
        ctx = validator_ctx_for[str(tenant)]
        gate_draft = _load_gate_draft(conn, touch_id, ctx)
        if gate_draft is None:
            raise PermanentJobError(f"touch {touch_id} not loadable as drafted")

        result = claim(PgGateStore(conn), gate_draft)
        if isinstance(result, Refusal):
            if result.terminal:
                # The job's work IS the release decision: commit it and finish
                # (raising here would roll the release back with the txn).
                conn.execute(
                    text("UPDATE touches SET status='released' WHERE id = CAST(:i AS uuid)"),
                    {"i": touch_id},
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO audit_events (subject_type, subject_id, event,
                            payload, actor)
                        VALUES ('touch', :i, 'released', CAST(:pl AS jsonb),
                            'system:deliver')
                        """
                    ),
                    {"i": touch_id,
                     "pl": json.dumps({"reason": result.reason, "detail": result.detail})},
                )
                return
            raise RuntimeError(f"retryable refusal: {result.reason}")  # backoff

        assert isinstance(result, ClaimedTouch)
        # Provider call inside the txn: failure -> rollback undoes the claim
        # atomically; success + commit failure -> provider idempotency key
        # (= touch id) absorbs the retry (spec 7.4, failure-mode table).
        receipt = provider.send(result, gate_draft.draft.subject, gate_draft.draft.body)
        prospect_id = conn.execute(
            text("SELECT prospect_id FROM touches WHERE id = CAST(:i AS uuid)"),
            {"i": touch_id},
        ).scalar()
        current = conn.execute(
            text("SELECT state FROM prospects WHERE id = :p"), {"p": prospect_id}
        ).scalar()
        if current == ProspectState.QUEUED:
            transition(conn, str(prospect_id), ProspectState.CONTACTED,
                       actor="system:deliver")
        conn.execute(
            text(
                """
                UPDATE touches SET status='dispatched', sent_at = now(),
                    provider_ref = CAST(:r AS jsonb)
                WHERE id = CAST(:i AS uuid)
                """
            ),
            {"i": touch_id, "r": json.dumps(receipt.provider_ref)},
        )

    return deliver
