"""Referral-ask flow (PRD FR-4.4, spec 8.14): strictly event-gated.

A referral ask fires only after a configured positive event (conversion or an
explicitly interested reply) — never attached to cold touches or neutral
replies. The `entities.referral_asked` flag makes the ask once-per-entity-EVER,
claimed under FOR UPDATE so concurrent positive events cannot double-ask.

Two modes (config `persona.referral.mode`):
- `direct`: we send the ask ourselves, composed through the full validator
  path and claimed under the REPLY gate profile (it continues an existing
  positive conversation; suppression/halt/validators still bind).
- `on_behalf_of`: the drafted colleague invite is delivered TO the converted
  provider as a human task — they send it (or it is sent visibly on their
  behalf with recorded consent). The framework never forges peer-to-peer mail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.agents.composer import ComposeEscalation, ComposeInputs, ComposeResult, compose
from open_reachout.core import dryrun, human_tasks
from open_reachout.core.escalations import escalate
from open_reachout.core.interfaces import Candidate, EvidenceCard, LLMBackend
from open_reachout.core.queue import Job
from open_reachout.core.sendpath import queue_draft
from open_reachout.core.worker import Handler

if TYPE_CHECKING:
    from open_reachout.core.config import PersonaSpec
    from open_reachout.core.prospecting import TenantRuntime


def make_referral_handler(
    runtimes: dict[str, TenantRuntime], llm: LLMBackend
) -> Handler:
    """referral-queue handler. Jobs are enqueued by the positive-event hooks
    (conversion, interested reply); everything here re-checks eligibility."""

    def referral(conn: Connection, job: Job) -> None:
        process_positive_event(conn, runtimes, llm, str(job.payload["prospect_id"]))

    return referral


def process_positive_event(
    conn: Connection, runtimes: dict[str, TenantRuntime], llm: LLMBackend,
    prospect_id: str,
) -> str | None:
    """Returns the queued touch id / human task id, or None when ineligible."""
    from open_reachout.core.prospecting import _load_for_referral

    runtime, persona, card, candidate = _load_for_referral(conn, runtimes, prospect_id)
    if runtime is None or persona is None or candidate is None or persona.referral is None:
        return None
    # Claim the once-ever flag atomically (FOR UPDATE via conditional UPDATE):
    # zero rows means another event already asked, or the entity is gone.
    claimed = conn.execute(
        text(
            """
            UPDATE entities SET referral_asked = true
            WHERE id = (SELECT entity_id FROM prospects WHERE id = CAST(:p AS uuid))
              AND referral_asked = false AND status = 'active'
            """
        ),
        {"p": prospect_id},
    ).rowcount
    if not claimed:
        return None

    spec = persona.referral
    if spec.mode == "on_behalf_of":
        result = _compose_result(conn, llm, runtime, persona, card, candidate,
                                 prospect_id, spec.prompt)
        if result is None:
            return None
        draft_text = result.draft.body
        return human_tasks.create_for_step(
            conn, tenant=runtime.config.tenant, prospect_id=prospect_id,
            campaign_id=f"{persona.id}:referral", step_index=0,
            instruction=(
                "Deliver this colleague-invite draft to the provider; THEY send "
                "it (or explicitly consent to visible on-behalf-of sending). "
                f"Draft:\n{draft_text}"
            ),
            value_prop=persona.value_prop,
        )

    result = _compose_result(conn, llm, runtime, persona, card, candidate,
                             prospect_id, spec.prompt)
    if result is None:
        return None
    return queue_draft(
        conn, prospect_id=prospect_id, campaign_id=f"{persona.id}:referral",
        variant_id=None, step_index=0, kind="agentic_reply",
        draft=result.draft, content_hash=result.content_sha256,
    )


def _compose_result(
    conn: Connection, llm: LLMBackend, runtime: TenantRuntime,
    persona: PersonaSpec, card: EvidenceCard, candidate: Candidate,
    prospect_id: str, prompt: str,
) -> ComposeResult | None:
    try:
        return compose(
            llm,
            ComposeInputs(
                variant_id=f"{persona.id}_referral",
                variant_prompt=prompt,
                values=dryrun.build_values(prompt, candidate, card, runtime.config, persona,
                                           sender_facts=runtime.sender_facts),
                validator_ctx=runtime.validator_ctx,
                trusted_context=dryrun.trusted_context(runtime.config, persona),
            ),
        )
    except (ComposeEscalation, KeyError) as exc:
        escalate(conn, tenant=runtime.config.tenant, subject_type="prospect",
                 subject_id=prospect_id, reason=f"referral compose failed: {exc}")
        return None
