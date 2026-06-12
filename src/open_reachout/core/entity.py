"""Entity resolution at ingest (PRD FR-2.4, spec section 12).

A discovered candidate resolves to an Entity — the human/org behind one or
more prospects — via deterministic keys (canonical email, NPI, place_id,
domain+phone). Cross-persona collisions (a venue owner who also gigs) land on
the same entity, so the frequency cap (I-7) and suppression apply across every
campaign that touches them. This is the structural answer to "don't pitch the
same person from two campaigns."

0.1 does deterministic resolution only; fuzzy name+postal matching (which
produces operator-reviewed merge proposals) is the documented next step.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core.canonical import InvalidEmailError, canonicalize
from open_reachout.core.interfaces import Candidate


def _phonedigits(phone: str | None) -> str:
    return "".join(c for c in (phone or "") if c.isdigit())[-10:]


def _domain(website: str | None) -> str:
    if not website:
        return ""
    host = website.split("//", 1)[-1].split("/", 1)[0].lower()
    return host[4:] if host.startswith("www.") else host


def deterministic_keys(candidate: Candidate) -> list[tuple[str, str]]:
    """(key_type, key_value) pairs that identify this candidate. Any exact
    match against an existing key resolves to that entity (spec 12.2)."""
    keys: list[tuple[str, str]] = []
    if candidate.email_raw:
        try:
            keys.append(("email_canonical", canonicalize(candidate.email_raw)))
        except InvalidEmailError:
            pass
    npi = candidate.source_ref.get("npi")
    if npi:
        keys.append(("npi", str(npi)))
    place_id = candidate.source_ref.get("place_id")
    if place_id:
        keys.append(("place_id", str(place_id)))
    domain, phone = _domain(candidate.website), _phonedigits(candidate.phone)
    if domain and phone:
        keys.append(("domain_phone", f"{domain}|{phone}"))
    return keys


@dataclass(frozen=True)
class Resolution:
    entity_id: str
    created: bool
    merge_conflict: bool  # keys pointed at >1 existing entity (spec 12.2)


def resolve_entity(conn: Connection, tenant_id: str, candidate: Candidate) -> Resolution:
    """Find or create the entity for a candidate, attaching its keys.

    If the candidate's keys already point at exactly one entity, attach any
    new keys and return it. If they point at *several* entities, that's a
    merge conflict — we attach to the first and flag it for an operator merge
    rather than silently fusing identities.
    """
    keys = deterministic_keys(candidate)
    matched: list[str] = []
    for key_type, key_value in keys:
        row = conn.execute(
            text(
                """
                SELECT e.id FROM entity_keys k JOIN entities e ON e.id = k.entity_id
                WHERE k.key_type = :kt AND k.key_value = :kv AND e.tenant_id = CAST(:t AS uuid)
                """
            ),
            {"kt": key_type, "kv": key_value, "t": tenant_id},
        ).fetchone()
        if row is not None and str(row[0]) not in matched:
            matched.append(str(row[0]))

    if matched:
        entity_id = matched[0]
    else:
        entity_id = str(uuid.uuid4())
        conn.execute(
            text(
                """
                INSERT INTO entities (id, tenant_id, display_name)
                VALUES (CAST(:i AS uuid), CAST(:t AS uuid), :n)
                """
            ),
            {"i": entity_id, "t": tenant_id, "n": candidate.display_name},
        )

    for key_type, key_value in keys:
        conn.execute(
            text(
                """
                INSERT INTO entity_keys (entity_id, key_type, key_value)
                VALUES (CAST(:e AS uuid), :kt, :kv)
                ON CONFLICT (key_type, key_value) DO NOTHING
                """
            ),
            {"e": entity_id, "kt": key_type, "kv": key_value},
        )
    return Resolution(entity_id=entity_id, created=not matched, merge_conflict=len(matched) > 1)
