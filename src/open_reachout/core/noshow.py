"""No-show handling (PRD FR-4.5, spec 8.14).

A missed booking (reported by the operator's calendar system via
`POST /v1/events {event_type: booking.no_show, selector: {entity_email}}`)
permits exactly ONE re-engagement touch after a configured delay — composed
through the full validator path. A second no-show closes the prospect:
`declined` with a 6-month suppression cooldown. The state machine has no edge
out of `declined` except `forget`, so rebooking loops are structurally
impossible.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core import queue
from open_reachout.core.escalations import escalate
from open_reachout.core.interfaces import LLMBackend
from open_reachout.core.lifecycle import transition
from open_reachout.core.queue import Job
from open_reachout.core.states import ProspectState, TransitionError
from open_reachout.core.worker import Handler

NOSHOW_EVENT = "booking.no_show"
REENGAGE_DELAY_DAYS = 3
COOLDOWN_MONTHS = 6
#: States in which a prospect can plausibly have a booking to miss.
ELIGIBLE_STATES = ("engaged", "contacted", "converted")


def process_no_show(
    conn: Connection, prospect_id: str, *, delay_days: int = REENGAGE_DELAY_DAYS
) -> str:
    """Record one no-show. First: schedule the single re-engagement. Second:
    close the prospect. Returns 'reengage_scheduled' | 'closed'."""
    prior = conn.execute(
        text(
            """
            SELECT count(*) FROM audit_events
            WHERE subject_type = 'prospect' AND subject_id = :p AND event = 'no_show'
            """
        ),
        {"p": prospect_id},
    ).scalar()
    conn.execute(
        text(
            """
            INSERT INTO audit_events (subject_type, subject_id, event, payload, actor)
            VALUES ('prospect', :p, 'no_show', '{}'::jsonb, 'system:noshow')
            """
        ),
        {"p": prospect_id},
    )
    if not prior:
        queue.enqueue(
            conn, "reengage", {"prospect_id": prospect_id},
            idempotency_key=f"reengage:{prospect_id}",
            run_after_seconds=delay_days * 86_400,
        )
        return "reengage_scheduled"
    # Second no-show: declined + cooldown; no infinite rebooking loops.
    email = conn.execute(
        text("SELECT email_canonical FROM prospects WHERE id = CAST(:p AS uuid)"),
        {"p": prospect_id},
    ).scalar()
    if email:
        conn.execute(
            text(
                """
                INSERT INTO suppressions (email_canonical, scope, reason, expires_at)
                VALUES (:e, 'global', 'no_show',
                        now() + make_interval(months => :m))
                ON CONFLICT (email_canonical, scope) DO UPDATE
                    SET expires_at = EXCLUDED.expires_at, reason = EXCLUDED.reason
                """
            ),
            {"e": email, "m": COOLDOWN_MONTHS},
        )
    try:
        transition(conn, prospect_id, ProspectState.DECLINED, actor="system:noshow",
                   reason="second no-show (FR-4.5)")
    except TransitionError:
        pass  # already terminal (e.g. unsubscribed meanwhile): nothing to close
    return "closed"


def make_reengage_handler(runtimes: dict[str, object], llm: LLMBackend) -> Handler:
    """reengage-queue handler: compose the one permitted re-engagement, or
    escalate when the persona has no reengagement prompt configured."""

    def reengage(conn: Connection, job: Job) -> None:
        prospect_id = str(job.payload["prospect_id"])
        from open_reachout.agents.composer import ComposeEscalation, ComposeInputs, compose
        from open_reachout.core import dryrun
        from open_reachout.core.prospecting import _persona_and_candidate
        from open_reachout.core.sendpath import queue_draft

        runtime, persona, candidate = _persona_and_candidate(
            conn, runtimes, prospect_id, ELIGIBLE_STATES  # type: ignore[arg-type]
        )
        if runtime is None or persona is None or candidate is None:
            return  # prospect exited (declined/unsubscribed) while we waited
        if persona.reengagement_prompt is None:
            escalate(
                conn, tenant=runtime.config.tenant, subject_type="prospect",
                subject_id=prospect_id,
                reason="booking no-show: re-engage manually (no reengagement_prompt "
                       "configured, FR-4.5)",
            )
            return
        from open_reachout.core.prospecting import _load_card

        card = _load_card(conn, prospect_id)
        try:
            result = compose(
                llm,
                ComposeInputs(
                    variant_id=f"{persona.id}_reengage",
                    variant_prompt=persona.reengagement_prompt,
                    values=dryrun.build_values(
                        persona.reengagement_prompt, candidate, card,
                        runtime.config, persona,
                    ),
                    validator_ctx=runtime.validator_ctx,
                    trusted_context=dryrun.trusted_context(runtime.config, persona),
                ),
            )
        except (ComposeEscalation, KeyError) as exc:
            escalate(conn, tenant=runtime.config.tenant, subject_type="prospect",
                     subject_id=prospect_id, reason=f"reengage compose failed: {exc}")
            return
        queue_draft(
            conn, prospect_id=prospect_id, campaign_id=f"{persona.id}:reengage",
            variant_id=None, step_index=0, kind="agentic_reply",
            draft=result.draft, content_hash=result.content_sha256,
        )

    return reengage


def handle_no_show_event(
    conn: Connection, tenant_id: str, selector: dict[str, object]
) -> int:
    """Resolve a booking.no_show operator event to prospects (FR-2.9 path).
    Returns the number of prospects processed."""
    from open_reachout.core.prospecting import _canon

    email = selector.get("entity_email")
    if not email:
        return 0
    canonical = _canon(str(email))
    if canonical is None:
        return 0
    rows = conn.execute(
        text(
            """
            SELECT id FROM prospects
            WHERE tenant_id = CAST(:t AS uuid) AND email_canonical = :e
              AND state = ANY(:s)
            """
        ),
        {"t": tenant_id, "e": canonical, "s": list(ELIGIBLE_STATES)},
    ).fetchall()
    for (pid,) in rows:
        process_no_show(conn, str(pid))
    return len(rows)
