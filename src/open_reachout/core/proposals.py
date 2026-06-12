"""Discovery-agent proposals (PRD FR-6.1/6.2): the propose->approve loop.

The discovery agent emits Proposals; a human approves or declines. Declines
are remembered for 90 days so the agent doesn't re-pitch the same direction.
Approving a proposal applies its delta; the always-human set (new personas,
value-prop claims) can only ever be `propose`, never auto-applied.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core import escalations  # HUMAN_ACTOR_PREFIX reuse

HUMAN_ACTOR_PREFIX = escalations.HUMAN_ACTOR_PREFIX
DECLINE_MEMORY_DAYS = 90

#: Kinds an auto-mode (hands_off) may apply without a human; everything else
#: always waits (FR-0.3 always-human set).
AUTO_APPLICABLE = frozenset({"budget_shift"})


@dataclass(frozen=True)
class Proposal:
    id: str
    tenant: str
    kind: str
    summary: str
    payload: dict
    evidence: dict
    created_at: datetime


def propose(
    conn: Connection,
    *,
    tenant: str,
    kind: str,
    summary: str,
    payload: dict,
    evidence: dict | None = None,
    dedupe_key: str | None = None,
) -> str | None:
    """Record a proposal. Returns its id, or None when suppressed by an open
    duplicate or a decline within the memory window (FR-6.2)."""
    if dedupe_key:
        existing = conn.execute(
            text(
                """
                SELECT 1 FROM proposals
                WHERE tenant = :t AND dedupe_key = :k
                  AND (status = 'open'
                       OR (status = 'declined'
                           AND resolved_at > now() - make_interval(days => :d)))
                LIMIT 1
                """
            ),
            {"t": tenant, "k": dedupe_key, "d": DECLINE_MEMORY_DAYS},
        ).fetchone()
        if existing is not None:
            return None
    row = conn.execute(
        text(
            """
            INSERT INTO proposals (tenant, kind, summary, payload, evidence, dedupe_key)
            VALUES (:t, :k, :s, CAST(:pl AS jsonb), CAST(:ev AS jsonb), :dk)
            RETURNING id
            """
        ),
        {"t": tenant, "k": kind, "s": summary, "pl": json.dumps(payload),
         "ev": json.dumps(evidence or {}), "dk": dedupe_key},
    ).fetchone()
    assert row is not None
    return str(row[0])


def list_open(conn: Connection, tenant: str | None = None) -> list[Proposal]:
    rows = conn.execute(
        text(
            """
            SELECT id, tenant, kind, summary, payload, evidence, created_at
            FROM proposals
            WHERE status = 'open' AND (CAST(:t AS text) IS NULL OR tenant = :t)
            ORDER BY created_at ASC
            """
        ),
        {"t": tenant},
    ).fetchall()
    return [Proposal(str(r[0]), r[1], r[2], r[3], r[4] or {}, r[5] or {}, r[6]) for r in rows]


def _get_open(conn: Connection, proposal_id: str) -> Proposal | None:
    row = conn.execute(
        text(
            """
            SELECT id, tenant, kind, summary, payload, evidence, created_at
            FROM proposals WHERE id = CAST(:i AS uuid) AND status = 'open'
            """
        ),
        {"i": proposal_id},
    ).fetchone()
    return None if row is None else Proposal(
        str(row[0]), row[1], row[2], row[3], row[4] or {}, row[5] or {}, row[6]
    )


def decline(conn: Connection, proposal_id: str, *, actor: str, note: str = "") -> bool:
    if not actor.startswith(HUMAN_ACTOR_PREFIX):
        raise PermissionError(f"declining a proposal requires a human actor, got {actor!r}")
    updated = conn.execute(
        text(
            """
            UPDATE proposals SET status='declined', resolved_at=now(), resolved_by=:a
            WHERE id = CAST(:i AS uuid) AND status='open'
            """
        ),
        {"i": proposal_id, "a": actor},
    ).rowcount
    if updated:
        _audit(conn, proposal_id, "declined", actor, {"note": note})
    return bool(updated)


def approve(conn: Connection, proposal_id: str, *, actor: str, auto: bool = False) -> bool:
    """Apply a proposal's delta. `auto=True` is the hands_off path and is only
    permitted for AUTO_APPLICABLE kinds; everything else requires a human."""
    if not auto and not actor.startswith(HUMAN_ACTOR_PREFIX):
        raise PermissionError(f"approving a proposal requires a human actor, got {actor!r}")
    proposal = _get_open(conn, proposal_id)
    if proposal is None:
        return False
    if auto and proposal.kind not in AUTO_APPLICABLE:
        raise PermissionError(
            f"proposal kind {proposal.kind!r} is always-human; cannot auto-apply (FR-0.3)"
        )
    _apply(conn, proposal)
    conn.execute(
        text(
            """
            UPDATE proposals SET status='approved', resolved_at=now(), resolved_by=:a
            WHERE id = CAST(:i AS uuid)
            """
        ),
        {"i": proposal_id, "a": actor},
    )
    _audit(conn, proposal_id, "approved", actor, {"kind": proposal.kind, "auto": auto})
    return True


def _apply(conn: Connection, proposal: Proposal) -> None:
    """Effect of approval. Only budget_shift mutates state in 0.1; new_cohort/
    value_prop require a config edit + re-validate (surfaced in the summary)."""
    if proposal.kind != "budget_shift":
        return  # recorded as approved; the operator edits config and re-validates
    from datetime import UTC

    period = datetime.now(UTC).strftime("%Y-%m")
    delta = int(proposal.payload["amount"])
    for scope_id, sign in ((proposal.payload["from_cohort"], -1),
                           (proposal.payload["to_cohort"], +1)):
        conn.execute(
            text(
                """
                UPDATE counters SET cap = GREATEST(cap + :d, 0)
                WHERE scope_type = 'cohort_month' AND scope_id = :s AND period = :p
                """
            ),
            {"d": sign * delta, "s": scope_id, "p": period},
        )


def _audit(conn: Connection, proposal_id: str, event: str, actor: str, payload: dict) -> None:
    conn.execute(
        text(
            """
            INSERT INTO audit_events (subject_type, subject_id, event, payload, actor)
            VALUES ('proposal', :i, :e, CAST(:pl AS jsonb), :a)
            """
        ),
        {"i": proposal_id, "e": event, "a": actor, "pl": json.dumps(payload)},
    )
