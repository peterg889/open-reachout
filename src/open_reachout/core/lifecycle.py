"""The only writer of prospects.state (spec 5.4). `forget` is the documented
exception: it bulk-transitions an entity's prospects inside its own
transaction with the same audit discipline."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core.states import ProspectState, assert_transition


def transition(
    conn: Connection,
    prospect_id: str,
    target: ProspectState,
    *,
    actor: str = "system",
    reason: str = "",
) -> None:
    current = conn.execute(
        text("SELECT state FROM prospects WHERE id = CAST(:i AS uuid) FOR UPDATE"),
        {"i": prospect_id},
    ).scalar()
    if current is None:
        raise LookupError(f"prospect {prospect_id} not found")
    assert_transition(ProspectState(current), target)
    conn.execute(
        text("UPDATE prospects SET state = :s WHERE id = CAST(:i AS uuid)"),
        {"s": target, "i": prospect_id},
    )
    conn.execute(
        text(
            """
            INSERT INTO audit_events (subject_type, subject_id, event, payload, actor)
            VALUES ('prospect', :i, 'transition', CAST(:pl AS jsonb), :a)
            """
        ),
        {"i": prospect_id, "a": actor,
         "pl": json.dumps({"from": current, "to": target, "reason": reason})},
    )
