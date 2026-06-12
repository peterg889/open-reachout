"""One-call data-subject deletion (PRD FR-1.4, invariant I-6, gate 5).

One local transaction: tombstone hashes -> PII deletion/scrubbing -> audit
receipt; then an async control-queue job propagates deletion to the sending
provider. Suppression rows containing the literal address are removed — the
tombstone hash is what survives, and `is_suppressed` checks it, so the person
stays permanently uncontactable without us retaining their address.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core import queue
from open_reachout.core.canonical import canonicalize, tombstone_hash


class UnknownSubjectError(LookupError):
    pass


@dataclass(frozen=True)
class ForgetReceipt:
    receipt_id: str
    entity_ids: list[str]
    addresses_tombstoned: int


def forget(conn: Connection, ref: str) -> ForgetReceipt:
    """Execute deletion for an email address or entity id (FR-1.4)."""
    if "@" in ref:
        canonical = canonicalize(ref)
        entity_ids = [
            str(r[0])
            for r in conn.execute(
                text(
                    """
                    SELECT DISTINCT entity_id FROM prospects WHERE email_canonical = :e
                    UNION
                    SELECT entity_id FROM entity_keys
                    WHERE key_type = 'email_canonical' AND key_value = :e
                    """
                ),
                {"e": canonical},
            ).fetchall()
        ]
        addresses = {canonical}
    else:
        entity_ids = [ref]
        addresses = set()

    if not entity_ids and not addresses:
        raise UnknownSubjectError(f"no entity found for {ref!r}")

    receipt_id = str(uuid.uuid4())

    # Collect every address tied to these entities before we delete anything.
    if entity_ids:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT email_canonical FROM prospects
                WHERE entity_id = ANY(CAST(:ids AS uuid[])) AND email_canonical IS NOT NULL
                """
            ),
            {"ids": entity_ids},
        ).fetchall()
        addresses |= {r[0] for r in rows}

    # 1. Tombstones (the only PII derivative that survives — a hash).
    for addr in sorted(addresses):
        conn.execute(
            text(
                """
                INSERT INTO forget_tombstones (email_hash, receipt_id)
                VALUES (:h, CAST(:r AS uuid)) ON CONFLICT (email_hash) DO NOTHING
                """
            ),
            {"h": tombstone_hash(addr), "r": receipt_id},
        )

    if entity_ids:
        ids = {"ids": entity_ids}
        # 2. Message contents survive as scrubbed skeletons (stats stay consistent).
        conn.execute(
            text(
                """
                UPDATE touches SET subject = NULL, body = NULL, scrubbed = true
                WHERE prospect_id IN
                  (SELECT id FROM prospects WHERE entity_id = ANY(CAST(:ids AS uuid[])))
                """
            ),
            ids,
        )
        conn.execute(
            text(
                """
                UPDATE replies SET body = NULL, scrubbed = true
                WHERE prospect_id IN
                  (SELECT id FROM prospects WHERE entity_id = ANY(CAST(:ids AS uuid[])))
                """
            ),
            ids,
        )
        # 3. Evidence and identity PII are deleted outright.
        conn.execute(
            text(
                """
                DELETE FROM evidence_facts WHERE prospect_id IN
                  (SELECT id FROM prospects WHERE entity_id = ANY(CAST(:ids AS uuid[])))
                """
            ),
            ids,
        )
        conn.execute(
            text(
                """
                UPDATE prospects SET email_raw = NULL, email_canonical = NULL,
                       source_ref = '{}'::jsonb, state = 'forgotten'
                WHERE entity_id = ANY(CAST(:ids AS uuid[]))
                """
            ),
            ids,
        )
        conn.execute(
            text("DELETE FROM entity_keys WHERE entity_id = ANY(CAST(:ids AS uuid[]))"), ids
        )
        conn.execute(
            text(
                """
                UPDATE entities SET display_name = NULL, status = 'forgotten'
                WHERE id = ANY(CAST(:ids AS uuid[]))
                """
            ),
            ids,
        )

    # 4. Remove suppression rows holding the literal address (tombstone covers it).
    for addr in sorted(addresses):
        conn.execute(
            text("DELETE FROM suppressions WHERE email_canonical = :e"), {"e": addr}
        )

    # 5. Audit receipt + provider propagation (async, with its own receipt update).
    conn.execute(
        text(
            """
            INSERT INTO audit_events (subject_type, subject_id, event, payload, actor)
            VALUES ('forget', :r, 'executed', CAST(:pl AS jsonb), 'operator:forget')
            """
        ),
        {"r": receipt_id,
         "pl": json.dumps({"entities": entity_ids, "addresses": len(addresses)})},
    )
    for addr in sorted(addresses):
        queue.enqueue(
            conn,
            "control",
            {"op": "delete_lead", "email_canonical": addr, "receipt_id": receipt_id},
            idempotency_key=f"forget:{tombstone_hash(addr)}",
        )
    return ForgetReceipt(
        receipt_id=receipt_id,
        entity_ids=entity_ids,
        addresses_tombstoned=len(addresses),
    )
