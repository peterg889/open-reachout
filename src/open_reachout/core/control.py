"""Halt and kill-switch flags (invariant I-2, gate 4).

Halt state persists in `control_flags` until an explicit human resume. The
scheduler, agents, and config reloads have no code path that writes here —
`set_flag` requires an actor, and `resume` rejects non-human actors outright.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core import queue

GLOBAL_SCOPE = "global"
HUMAN_ACTOR_PREFIX = "operator:"


class ResumeRequiresHumanError(PermissionError):
    pass


def halt(conn: Connection, *, scope: str = GLOBAL_SCOPE, actor: str, flag: str = "halted") -> None:
    """Set a halt/kill-switch flag and fan out provider pauses (spec 7.6).

    Kill switches pass actor='system:killswitch'; resume still requires a human.
    """
    conn.execute(
        text(
            """
            INSERT INTO control_flags (scope, flag, set_by)
            VALUES (:s, :f, :a)
            ON CONFLICT (scope) DO UPDATE SET flag = EXCLUDED.flag,
                set_by = EXCLUDED.set_by, set_at = now()
            """
        ),
        {"s": scope, "f": flag, "a": actor},
    )
    queue.enqueue(
        conn,
        "control",
        {"op": "pause_all_campaigns", "scope": scope},
        idempotency_key=f"halt:{scope}:{flag}",
    )


def resume(conn: Connection, *, scope: str = GLOBAL_SCOPE, actor: str) -> bool:
    """Clear a flag. Only a human operator may do this (I-2) — no exceptions,
    including for flags the system set itself."""
    if not actor.startswith(HUMAN_ACTOR_PREFIX):
        raise ResumeRequiresHumanError(
            f"resume requires a human operator actor, got {actor!r} (invariant I-2)"
        )
    result = conn.execute(text("DELETE FROM control_flags WHERE scope = :s"), {"s": scope})
    conn.execute(
        text(
            """
            INSERT INTO audit_events (subject_type, subject_id, event, actor)
            VALUES ('control_flag', :s, 'resume', :a)
            """
        ),
        {"s": scope, "a": actor},
    )
    return bool(result.rowcount)


def halted_scopes(conn: Connection, tenant: str) -> list[str]:
    rows = conn.execute(
        text("SELECT scope FROM control_flags WHERE scope IN (:g, :t)"),
        {"g": GLOBAL_SCOPE, "t": tenant},
    ).fetchall()
    return [r[0] for r in rows]
