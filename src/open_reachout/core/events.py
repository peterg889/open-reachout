"""Provider event ingestion (spec 8.5, invariant I-10).

Webhook -> signature-verified parse -> idempotent provider_events insert ->
deterministic handling for bounce/complaint/unsubscribe (no LLM, I-11) and a
classify job for replies.
"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core import queue, suppression
from open_reachout.core.interfaces import EventKind, LLMBackend, ProviderEvent, SendingProvider
from open_reachout.core.lifecycle import transition
from open_reachout.core.queue import Job
from open_reachout.core.replies import Action, route
from open_reachout.core.states import ProspectState, TransitionError
from open_reachout.core.worker import Handler, PermanentJobError

PROVIDER_NAME = "provider"  # single-provider deployments; adapter name otherwise


def ingest_webhook(
    conn: Connection, provider: SendingProvider, payload: bytes, signature: str
) -> int:
    """Verify, dedupe, and act on a webhook. Returns events newly processed.

    Raises WebhookVerificationError on bad signatures (gate 13): drop + alert
    is the caller's job; nothing unverified reaches this far.
    """
    events = provider.parse_webhook(payload, signature)
    processed = 0
    for event in events:
        inserted = conn.execute(
            text(
                """
                INSERT INTO provider_events (provider, provider_event_id, kind, payload)
                VALUES (:p, :e, :k, CAST(:pl AS jsonb))
                ON CONFLICT (provider, provider_event_id) DO NOTHING
                RETURNING id
                """
            ),
            {"p": PROVIDER_NAME, "e": event.provider_event_id, "k": event.kind,
             "pl": json.dumps(event.payload)},
        ).fetchone()
        if inserted is None:
            continue  # duplicate delivery: no-op (I-10)
        _act(conn, event)
        processed += 1
    return processed


def _prospect_for(conn: Connection, event: ProviderEvent) -> tuple[str, str, str] | None:
    """(prospect_id, tenant_slug, email_canonical) from the event's touch ref."""
    touch_id = event.touch_ref.get("touch_id")
    if not touch_id:
        return None
    row = conn.execute(
        text(
            """
            SELECT p.id, t.slug, p.email_canonical
            FROM touches tc
            JOIN prospects p ON p.id = tc.prospect_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE tc.id = CAST(:i AS uuid)
            """
        ),
        {"i": touch_id},
    ).fetchone()
    return None if row is None else (str(row[0]), row[1], row[2])


def _safe_transition(conn: Connection, prospect_id: str, target: ProspectState) -> None:
    """Tolerant transition for safety-event paths: a late bounce on an engaged
    prospect must never roll back its own suppression. The skip is audited."""
    try:
        transition(conn, prospect_id, target, actor="system:events")
    except TransitionError as exc:
        conn.execute(
            text(
                """
                INSERT INTO audit_events (subject_type, subject_id, event, payload, actor)
                VALUES ('prospect', :i, 'transition_skipped', CAST(:pl AS jsonb),
                        'system:events')
                """
            ),
            {"i": prospect_id, "pl": json.dumps({"target": target, "why": str(exc)})},
        )


def _act(conn: Connection, event: ProviderEvent) -> None:
    """Deterministic event handling (I-11: no LLM on these paths)."""
    ref = _prospect_for(conn, event)
    if event.kind is EventKind.BOUNCE and ref:
        prospect_id, _tenant, email = ref
        if email:
            suppression.suppress(conn, email, reason="bounce")
        _safe_transition(conn, prospect_id, ProspectState.BOUNCED)
    elif event.kind is EventKind.COMPLAINT and ref:
        prospect_id, _tenant, email = ref
        if email:
            suppression.suppress(conn, email, reason="complaint")
        _safe_transition(conn, prospect_id, ProspectState.DECLINED)
    elif event.kind is EventKind.UNSUBSCRIBE and ref:
        prospect_id, _tenant, email = ref
        if email:
            suppression.suppress(conn, email, reason="unsubscribe")
        _safe_transition(conn, prospect_id, ProspectState.UNSUBSCRIBED)
    elif event.kind is EventKind.REPLY and ref:
        prospect_id, _tenant, _email = ref
        reply_id = conn.execute(
            text(
                """
                INSERT INTO replies (prospect_id, touch_id, body)
                VALUES (CAST(:p AS uuid), CAST(:t AS uuid), :b) RETURNING id
                """
            ),
            {"p": prospect_id, "t": event.touch_ref.get("touch_id"),
             "b": str(event.payload.get("body", ""))},
        ).scalar()
        queue.enqueue(
            conn, "classify", {"reply_id": str(reply_id)},
            idempotency_key=f"classify:{reply_id}",
        )
        _safe_transition(conn, prospect_id, ProspectState.ENGAGED)
    elif event.kind is EventKind.SENT and event.touch_ref.get("touch_id"):
        conn.execute(
            text(
                """
                UPDATE touches SET status = 'sent'
                WHERE id = CAST(:i AS uuid) AND status = 'dispatched'
                """
            ),
            {"i": event.touch_ref["touch_id"]},
        )


def make_classify_handler(llm: LLMBackend) -> Handler:
    """classify-queue handler: route the reply, apply deterministic outcomes,
    record everything. Outbound agentic responses are queued as drafted
    touches by M3's reply composer; here we record + suppress + escalate."""

    def classify(conn: Connection, job: Job) -> None:
        reply_id = str(job.payload["reply_id"])
        row = conn.execute(
            text(
                """
                SELECT r.body, r.agentic_exchanges, p.id, p.email_canonical, t.slug
                FROM replies r
                JOIN prospects p ON p.id = r.prospect_id
                JOIN tenants t ON t.id = p.tenant_id
                WHERE r.id = CAST(:i AS uuid)
                """
            ),
            {"i": reply_id},
        ).fetchone()
        if row is None:
            raise PermanentJobError(f"reply {reply_id} not found")
        body, exchanges, prospect_id, email, _tenant = row
        if body is None:
            return  # scrubbed by forget: nothing to classify

        decision = route(body, llm, agentic_exchanges=exchanges)
        conn.execute(
            text(
                """
                UPDATE replies SET intent = :it, confidence = :c
                WHERE id = CAST(:i AS uuid)
                """
            ),
            {"it": decision.intent, "c": decision.confidence, "i": reply_id},
        )
        if decision.action is Action.SUPPRESS_UNSUBSCRIBE and email:
            suppression.suppress(conn, email, reason="unsubscribe")
            _safe_transition(conn, str(prospect_id), ProspectState.UNSUBSCRIBED)
        elif decision.action is Action.CLOSE_POLITE_SUPPRESS and email:
            # 12-month suppression (FR-4.2)
            conn.execute(
                text(
                    """
                    INSERT INTO suppressions (email_canonical, scope, reason, expires_at)
                    VALUES (:e, 'global', 'declined', now() + interval '12 months')
                    ON CONFLICT (email_canonical, scope) DO UPDATE
                        SET expires_at = EXCLUDED.expires_at, reason = EXCLUDED.reason
                    """
                ),
                {"e": email},
            )
            _safe_transition(conn, str(prospect_id), ProspectState.DECLINED)
        elif decision.action is Action.ESCALATE:
            conn.execute(
                text(
                    """
                    INSERT INTO audit_events (subject_type, subject_id, event,
                        payload, actor)
                    VALUES ('reply', :i, 'escalated', CAST(:pl AS jsonb), 'system:classify')
                    """
                ),
                {"i": reply_id, "pl": json.dumps({"reason": decision.reason,
                                                  "intent": decision.intent})},
            )

    return classify


def make_control_handler(provider: SendingProvider) -> Handler:
    """control-queue handler: reactive provider enforcement (spec 7.6) —
    highest priority, spend-exempt (I-11)."""

    def control_op(conn: Connection, job: Job) -> None:
        op = job.payload.get("op")
        if op == "pause_lead":
            provider.pause_lead(str(job.payload["email_canonical"]))
        elif op == "delete_lead":
            provider.pause_lead(str(job.payload["email_canonical"]))
            conn.execute(
                text(
                    """
                    UPDATE forget_tombstones SET provider_propagated_at = now()
                    WHERE receipt_id = CAST(:r AS uuid)
                    """
                ),
                {"r": job.payload["receipt_id"]},
            )
        elif op == "pause_all_campaigns":
            provider.pause_all_campaigns(str(job.payload["scope"]))
        else:
            raise PermanentJobError(f"unknown control op {op!r}")

    return control_op
