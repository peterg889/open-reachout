"""Suppression service (invariant I-3, gates 3/5/8): alias-aware, tombstone-aware.

`is_suppressed` consults both the suppressions table and forget tombstones —
a forgotten person stays uncontactable even after their address is deleted
(the hash is all that survives, and it is enough).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core import queue
from open_reachout.core.canonical import canonicalize, tombstone_hash

GLOBAL_SCOPE = "global"


def suppress(
    conn: Connection,
    email: str,
    *,
    scope: str = GLOBAL_SCOPE,
    reason: str,
    expires_at: datetime | None = None,
    propagate: bool = True,
) -> str:
    """Insert/refresh a suppression and enqueue provider propagation (spec 13.2).

    Returns the canonical address. The control-queue job pauses/deletes the
    lead at the sending provider — highest priority, spend-exempt (I-11).
    """
    canonical = canonicalize(email)
    conn.execute(
        text(
            """
            INSERT INTO suppressions (email_canonical, scope, reason, expires_at)
            VALUES (:e, :s, :r, :x)
            ON CONFLICT (email_canonical, scope)
            DO UPDATE SET reason = EXCLUDED.reason, expires_at = EXCLUDED.expires_at,
                          created_at = now()
            """
        ),
        {"e": canonical, "s": scope, "r": reason, "x": expires_at},
    )
    if propagate:
        queue.enqueue(
            conn,
            "control",
            {"op": "pause_lead", "email_canonical": canonical, "scope": scope},
            idempotency_key=f"pause:{canonical}:{scope}:{reason}",
        )
    return canonical


def is_suppressed(conn: Connection, email_canonical: str, tenant: str) -> bool:
    row = conn.execute(
        text(
            """
            SELECT 1 FROM suppressions
            WHERE email_canonical = :e AND scope IN (:g, :t)
              AND (expires_at IS NULL OR expires_at > now())
            UNION ALL
            SELECT 1 FROM forget_tombstones WHERE email_hash = :h
            LIMIT 1
            """
        ),
        {"e": email_canonical, "g": GLOBAL_SCOPE, "t": tenant,
         "h": tombstone_hash(email_canonical)},
    ).fetchone()
    return row is not None


def screen_at_ingest(conn: Connection, email_raw: str, tenant: str) -> bool:
    """True if a discovered candidate may proceed (gate 5: re-discovered
    forgotten prospects are dropped silently)."""
    try:
        canonical = canonicalize(email_raw)
    except ValueError:
        return False  # unparseable addresses fail closed
    return not is_suppressed(conn, canonical, tenant)
