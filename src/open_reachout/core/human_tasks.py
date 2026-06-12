"""Human tasks as first-class sequence steps (PRD FR-3.6, spec 8.13).

A sequence step may be off-channel ("DM them on Instagram", "walk in
Thursday"): the framework assembles a complete task brief — entity context,
Evidence Card facts, conversation history, the persona's value prop — into
the operator queue and parks the sequence. Two rules with teeth:

- A completed human touch counts against the entity's frequency caps exactly
  like an email (off-channel contact is still contact, invariant I-7).
- Tasks expire after EXPIRE_DAYS into `skipped` so a forgotten task cannot
  park a prospect forever.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core.escalations import HUMAN_ACTOR_PREFIX

EXPIRE_DAYS = 14


@dataclass(frozen=True)
class HumanTask:
    id: str
    tenant: str
    prospect_id: str
    instruction: str
    brief: str
    created_at: datetime


def _assemble_brief(
    conn: Connection, prospect_id: str, instruction: str, value_prop: str
) -> str:
    """Deterministic task brief: everything the operator needs to do the touch
    well, from data already in the store. (No LLM: briefs must work when
    models are down or budgets exhausted, and they cite, not compose.)"""
    row = conn.execute(
        text(
            """
            SELECT e.display_name, p.email_canonical, p.cohort_id
            FROM prospects p JOIN entities e ON e.id = p.entity_id
            WHERE p.id = CAST(:p AS uuid)
            """
        ),
        {"p": prospect_id},
    ).fetchone()
    name, email, cohort = row if row else ("(unknown)", None, "?")
    facts = conn.execute(
        text(
            """
            SELECT fact_type, content, source_url, observed_at FROM evidence_facts
            WHERE prospect_id = CAST(:p AS uuid) ORDER BY observed_at DESC LIMIT 8
            """
        ),
        {"p": prospect_id},
    ).fetchall()
    history = conn.execute(
        text(
            """
            SELECT kind, status, subject, sent_at FROM touches
            WHERE prospect_id = CAST(:p AS uuid) ORDER BY sent_at NULLS LAST LIMIT 8
            """
        ),
        {"p": prospect_id},
    ).fetchall()
    lines = [
        f"TASK: {instruction}",
        f"Who: {name}" + (f" <{email}>" if email else ""),
        f"Cohort: {cohort}",
        f"Why we reach out: {value_prop}",
        "",
        "Evidence (cite-able, with provenance):",
    ]
    lines += [
        f"- [{f[0]}] {json.dumps(f[1])} ({f[2]}, observed {f[3]:%Y-%m-%d})"
        for f in facts
    ] or ["- (no evidence card yet)"]
    lines += ["", "Prior contact:"]
    lines += [
        f"- {t[0]}/{t[1]}: {t[2] or '(no subject)'}"
        + (f" sent {t[3]:%Y-%m-%d}" if t[3] else "")
        for t in history
    ] or ["- (none)"]
    return "\n".join(lines)


def create_for_step(
    conn: Connection,
    *,
    tenant: str,
    prospect_id: str,
    campaign_id: str,
    step_index: int,
    instruction: str,
    value_prop: str,
) -> str:
    """Create the parked touch + the operator task. Returns the task id."""
    touch_id = str(uuid.uuid4())
    conn.execute(
        text(
            """
            INSERT INTO touches (id, prospect_id, campaign_id, variant_id, step_index,
                kind, status, subject, body, content_hash, idempotency_key)
            VALUES (CAST(:i AS uuid), CAST(:p AS uuid), :c, NULL, :s, 'human_task',
                'pending_human', :instr, NULL, 'human_task', :i)
            """
        ),
        {"i": touch_id, "p": prospect_id, "c": campaign_id, "s": step_index,
         "instr": instruction},
    )
    brief = _assemble_brief(conn, prospect_id, instruction, value_prop)
    row = conn.execute(
        text(
            """
            INSERT INTO human_tasks (tenant, prospect_id, touch_id, instruction, brief)
            VALUES (:t, CAST(:p AS uuid), CAST(:ti AS uuid), :ins, :b)
            RETURNING id
            """
        ),
        {"t": tenant, "p": prospect_id, "ti": touch_id, "ins": instruction, "b": brief},
    ).fetchone()
    assert row is not None
    task_id = str(row[0])
    _audit(conn, task_id, "created", "system:compose", {"touch_id": touch_id})
    return task_id


def list_pending(conn: Connection, tenant: str | None = None) -> list[HumanTask]:
    rows = conn.execute(
        text(
            """
            SELECT id, tenant, prospect_id, instruction, brief, created_at
            FROM human_tasks
            WHERE status = 'pending' AND (CAST(:t AS text) IS NULL OR tenant = :t)
            ORDER BY created_at ASC
            """
        ),
        {"t": tenant},
    ).fetchall()
    return [HumanTask(str(r[0]), r[1], str(r[2]), r[3], r[4], r[5]) for r in rows]


def resolve(
    conn: Connection, task_id: str, *, actor: str, done: bool, note: str = ""
) -> bool:
    """Mark a task done (counts as contact, I-7) or skipped (touch released)."""
    if not actor.startswith(HUMAN_ACTOR_PREFIX):
        raise PermissionError(f"resolving a human task requires a human actor, got {actor!r}")
    row = conn.execute(
        text(
            """
            UPDATE human_tasks SET status = :s, resolved_at = now(), resolved_by = :a,
                outcome_note = :n
            WHERE id = CAST(:i AS uuid) AND status = 'pending'
            RETURNING touch_id, prospect_id
            """
        ),
        {"i": task_id, "s": "done" if done else "skipped", "a": actor, "n": note},
    ).fetchone()
    if row is None:
        return False
    touch_id, prospect_id = str(row[0]), str(row[1])
    if done:
        # The off-channel contact happened: the touch is sent and the entity's
        # frequency-governance state advances exactly as for an email.
        conn.execute(
            text(
                """
                UPDATE touches SET status = 'sent', sent_at = now()
                WHERE id = CAST(:i AS uuid)
                """
            ),
            {"i": touch_id},
        )
        conn.execute(
            text(
                """
                UPDATE entities SET last_campaign_contact_at = now(),
                    touches_12mo = touches_12mo + 1
                WHERE id = (SELECT entity_id FROM prospects WHERE id = CAST(:p AS uuid))
                """
            ),
            {"p": prospect_id},
        )
    else:
        conn.execute(
            text("UPDATE touches SET status = 'released' WHERE id = CAST(:i AS uuid)"),
            {"i": touch_id},
        )
    _audit(conn, task_id, "done" if done else "skipped", actor, {"note": note})
    return True


def expire(conn: Connection, *, max_age_days: int = EXPIRE_DAYS) -> int:
    """Expire stale pending tasks (spec 8.13: a forgotten task must not park a
    prospect forever). Their touches are released, never counted as contact."""
    rows = conn.execute(
        text(
            """
            UPDATE human_tasks SET status = 'expired', resolved_at = now(),
                resolved_by = 'system:expiry'
            WHERE status = 'pending'
              AND created_at < now() - make_interval(days => :d)
            RETURNING id, touch_id
            """
        ),
        {"d": max_age_days},
    ).fetchall()
    for task_id, touch_id in rows:
        conn.execute(
            text("UPDATE touches SET status = 'released' WHERE id = CAST(:i AS uuid)"),
            {"i": str(touch_id)},
        )
        _audit(conn, str(task_id), "expired", "system:expiry", {})
    return len(rows)


def _audit(
    conn: Connection, task_id: str, event: str, actor: str, payload: dict[str, object]
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO audit_events (subject_type, subject_id, event, payload, actor)
            VALUES ('human_task', :i, :e, CAST(:pl AS jsonb), :a)
            """
        ),
        {"i": task_id, "e": event, "a": actor, "pl": json.dumps(payload)},
    )
