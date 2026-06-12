"""The escalation queue (PRD FR-4.6, RX-1): where the agent hands off to a
human. Resolving requires a human actor, mirroring halt-resume semantics."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

HUMAN_ACTOR_PREFIX = "operator:"


@dataclass(frozen=True)
class Escalation:
    id: str
    tenant: str | None
    subject_type: str
    subject_id: str
    reason: str
    payload: dict
    created_at: datetime


def escalate(
    conn: Connection,
    *,
    tenant: str | None,
    subject_type: str,
    subject_id: str,
    reason: str,
    payload: dict | None = None,
) -> str:
    row = conn.execute(
        text(
            """
            INSERT INTO escalations (tenant, subject_type, subject_id, reason, payload)
            VALUES (:t, :st, :si, :r, CAST(:pl AS jsonb))
            RETURNING id
            """
        ),
        {"t": tenant, "st": subject_type, "si": subject_id, "r": reason,
         "pl": json.dumps(payload or {})},
    ).fetchone()
    assert row is not None
    return str(row[0])


def list_open(conn: Connection, tenant: str | None = None) -> list[Escalation]:
    rows = conn.execute(
        text(
            """
            SELECT id, tenant, subject_type, subject_id, reason, payload, created_at
            FROM escalations
            WHERE status = 'open' AND (CAST(:t AS text) IS NULL OR tenant = :t)
            ORDER BY created_at ASC
            """
        ),
        {"t": tenant},
    ).fetchall()
    return [
        Escalation(str(r[0]), r[1], r[2], r[3], r[4], r[5] or {}, r[6]) for r in rows
    ]


def resolve(conn: Connection, escalation_id: str, *, actor: str, note: str = "") -> bool:
    if not actor.startswith(HUMAN_ACTOR_PREFIX):
        raise PermissionError(f"resolving escalations requires a human actor, got {actor!r}")
    updated = conn.execute(
        text(
            """
            UPDATE escalations SET status = 'resolved', resolved_at = now(), resolved_by = :a
            WHERE id = CAST(:i AS uuid) AND status = 'open'
            """
        ),
        {"i": escalation_id, "a": actor},
    ).rowcount
    if updated:
        conn.execute(
            text(
                """
                INSERT INTO audit_events (subject_type, subject_id, event, payload, actor)
                VALUES ('escalation', :i, 'resolved', CAST(:pl AS jsonb), :a)
                """
            ),
            {"i": escalation_id, "a": actor, "pl": json.dumps({"note": note})},
        )
    return bool(updated)
