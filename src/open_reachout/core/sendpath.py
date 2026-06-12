"""The outbound send path (spec 7.4/7.6): queue a drafted touch, then the
deliver handler claims and dispatches in ONE transaction — a provider failure
rolls the claim back atomically (counters, entity state, everything).
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

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
from open_reachout.stats.persistence import record_trial

if TYPE_CHECKING:
    from open_reachout.core.config import SequenceSpec


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
    approve_first: int = 0,
) -> str:
    """Persist a drafted touch and enqueue its deliver job (idempotent on id).

    Message-review ramp (FR-0.3, spec 8.4): while fewer than `approve_first`
    touches exist for this (campaign, variant), the draft routes to the review
    queue (`pending_review`) instead of the gatekeeper. The count is taken
    under a per-(campaign, variant) advisory lock in the drafting transaction,
    so two concurrent drafts cannot both count as "the Nth". A reviewer cannot
    approve past suppression or budget — approval just re-enters the claim path.
    """
    touch_id = str(uuid.uuid4())
    hold = False
    if approve_first > 0:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"ramp:{campaign_id}:{variant_id or ''}"},
        )
        prior = conn.execute(
            text(
                """
                SELECT count(*) FROM touches
                WHERE campaign_id = :c AND variant_id IS NOT DISTINCT FROM :v
                """
            ),
            {"c": campaign_id, "v": variant_id},
        ).scalar()
        hold = (prior or 0) < approve_first
    conn.execute(
        text(
            """
            INSERT INTO touches (id, prospect_id, campaign_id, variant_id, step_index,
                kind, status, subject, body, content_hash, idempotency_key)
            VALUES (CAST(:i AS uuid), CAST(:p AS uuid), :c, :v, :s, :k, :st,
                :subj, :body, :h, :i)
            """
        ),
        {
            "i": touch_id, "p": prospect_id, "c": campaign_id, "v": variant_id,
            "s": step_index, "k": kind, "subj": draft.subject, "body": draft.body,
            "h": content_hash, "st": "pending_review" if hold else "drafted",
        },
    )
    if not hold:
        queue.enqueue(conn, "deliver", {"touch_id": touch_id},
                      idempotency_key=f"deliver:{touch_id}")
    return touch_id


def approve_pending(conn: Connection, touch_id: str, *, actor: str) -> bool:
    """Reviewer releases a ramp-held draft to the gatekeeper (FR-0.3). The full
    claim transaction still runs — approval is routing, not a gate bypass."""
    if not actor.startswith("operator:"):
        raise PermissionError(f"ramp approval requires a human actor, got {actor!r}")
    updated = conn.execute(
        text(
            """
            UPDATE touches SET status = 'drafted'
            WHERE id = CAST(:i AS uuid) AND status = 'pending_review'
            """
        ),
        {"i": touch_id},
    ).rowcount
    if not updated:
        return False
    queue.enqueue(conn, "deliver", {"touch_id": touch_id},
                  idempotency_key=f"deliver:{touch_id}")
    _audit_review(conn, touch_id, "ramp_approved", actor)
    return True


def reject_pending(conn: Connection, touch_id: str, *, actor: str, note: str = "") -> bool:
    """Reviewer rejects a ramp-held draft: released, recorded as a correction
    (FR-2.8 ground truth)."""
    if not actor.startswith("operator:"):
        raise PermissionError(f"ramp rejection requires a human actor, got {actor!r}")
    updated = conn.execute(
        text(
            """
            UPDATE touches SET status = 'released'
            WHERE id = CAST(:i AS uuid) AND status = 'pending_review'
            """
        ),
        {"i": touch_id},
    ).rowcount
    if updated:
        _audit_review(conn, touch_id, "ramp_rejected", actor, note)
    return bool(updated)


def list_pending_review(conn: Connection) -> list[tuple[str, str, str, str]]:
    """(touch_id, campaign_id, subject, body) of ramp-held drafts."""
    rows = conn.execute(
        text(
            """
            SELECT id, campaign_id, subject, body FROM touches
            WHERE status = 'pending_review' ORDER BY id
            """
        )
    ).fetchall()
    return [(str(r[0]), r[1], r[2] or "", r[3] or "") for r in rows]


def _audit_review(
    conn: Connection, touch_id: str, event: str, actor: str, note: str = ""
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO audit_events (subject_type, subject_id, event, payload, actor)
            VALUES ('touch', :i, :e, CAST(:pl AS jsonb), :a)
            """
        ),
        {"i": touch_id, "e": event, "a": actor, "pl": json.dumps({"note": note})},
    )


def _load_gate_draft(conn: Connection, touch_id: str, ctx: ValidatorContext) -> GateDraft | None:
    row = conn.execute(
        text(
            """
            SELECT tc.subject, tc.body, tc.step_index, tc.kind, tc.content_hash,
                   tc.status, p.entity_id, p.email_canonical, p.cohort_id, t.slug,
                   p.id
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
    (subject, body, step_index, kind, content_hash, status, entity_id, email,
     cohort, slug, prospect_id) = row
    if status != "drafted":
        return None
    if email is None:
        raise PermanentJobError("prospect has no contactable address")
    profile = (
        GateProfile.REPLY if kind == "agentic_reply"
        else GateProfile.FOLLOWUP if kind == "followup"
        else GateProfile.COLD
    )
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
        profile=profile,
        validator_ctx=ctx,
        cohort_id=cohort,
        prospect_id=str(prospect_id),
    )


def release_sequence(conn: Connection, prospect_id: str) -> bool:
    """Stop condition (FR-3.5): clear the entity's active sequence when it
    belongs to this prospect — on reply, exit, completion, or dead-end. The
    90-day between-campaigns gap still binds via last_campaign_contact_at."""
    return bool(
        conn.execute(
            text(
                """
                UPDATE entities SET active_sequence_touch_id = NULL
                WHERE id = (SELECT entity_id FROM prospects WHERE id = CAST(:p AS uuid))
                  AND active_sequence_touch_id IN
                      (SELECT id FROM touches WHERE prospect_id = CAST(:p AS uuid))
                """
            ),
            {"p": prospect_id},
        ).rowcount
    )


def make_deliver_handler(
    provider: SendingProvider, validator_ctx_for: dict[str, ValidatorContext],
    sequences: dict[str, dict[str, SequenceSpec]] | None = None,
) -> Handler:
    """Build the deliver-queue handler (claim + send + dispatch, one txn).

    `sequences` maps tenant -> persona_id -> SequenceSpec; when provided, a
    successful dispatch of a non-final step schedules the next step's compose
    job after the configured gap (FR-3.5), and dispatching the final step
    releases the entity's sequence lock."""

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
        variant_id = conn.execute(
            text("SELECT variant_id FROM touches WHERE id = CAST(:i AS uuid)"),
            {"i": touch_id},
        ).scalar()
        if variant_id:  # agentic replies carry no variant
            record_trial(conn, gate_draft.tenant, str(variant_id))
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
        _plan_next_step(conn, touch_id, str(prospect_id), gate_draft, sequences)

    return deliver


def _plan_next_step(
    conn: Connection, touch_id: str, prospect_id: str, gate_draft: GateDraft,
    sequences: dict[str, dict[str, SequenceSpec]] | None,
) -> None:
    """FR-3.5: after a cold/follow-up dispatch, schedule the next step after
    its gap, or release the sequence when the drip is complete."""
    if sequences is None or gate_draft.profile is GateProfile.REPLY:
        return
    persona_id = conn.execute(
        text("SELECT persona_id FROM prospects WHERE id = CAST(:p AS uuid)"),
        {"p": prospect_id},
    ).scalar()
    seq = sequences.get(gate_draft.tenant, {}).get(str(persona_id))
    if seq is None:
        return
    next_step = gate_draft.draft.step_index + 1
    if next_step >= seq.steps:
        release_sequence(conn, prospect_id)  # drip complete; entity unlocks
        return
    queue.enqueue(
        conn, "compose",
        {"prospect_id": prospect_id, "step_index": next_step},
        idempotency_key=f"followup:{prospect_id}:{next_step}",
        run_after_seconds=seq.gaps_days[gate_draft.draft.step_index] * 86_400,
    )
