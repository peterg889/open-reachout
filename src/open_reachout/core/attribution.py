"""Conversion attribution (PRD FR-8.3, gate 12).

Every outbound link carries a signed touch token; the conversion endpoint
verifies the MAC and attributes tenant -> persona -> cohort -> variant ->
touch — closing the CAC loop and feeding TRUE conversions (not just replies)
into the bandit. Invalid MACs change nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core.lifecycle import transition
from open_reachout.core.states import ProspectState, TransitionError
from open_reachout.stats.persistence import record_success

MAC_LEN = 12
ATTRIBUTION_KEY_ENV = "OR_ATTRIBUTION_KEY"

_URL_RE = re.compile(r"https?://[^\s>\")\]]+", re.IGNORECASE)


def key_from_env() -> bytes | None:
    """The deployment attribution key, or None when unset (links go bare)."""
    raw = os.environ.get(ATTRIBUTION_KEY_ENV, "")
    return raw.encode() or None


def tokenize_links(body: str, touch_id: str, key: bytes) -> str:
    """Append the signed touch token to every URL in the body (FR-8.3) so a
    conversion on the operator's side attributes back to this exact touch.
    Runs at queue time — BEFORE the content hash is computed — so the bound
    content is exactly what dispatches (validate-then-bind, spec 7.3)."""
    token = token_for(touch_id, key)

    def _append(match: re.Match[str]) -> str:
        url = match.group(0)
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}t={token}"

    return _URL_RE.sub(_append, body)


def token_for(touch_id: str, key: bytes) -> str:
    """`<touch-uuid-hex>.<mac>` — append as ?t= on tenant links."""
    canonical = uuid.UUID(touch_id).hex
    mac = hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()[:MAC_LEN]
    return f"{canonical}.{mac}"


def verify(token: str, key: bytes) -> str | None:
    """Returns the touch id, or None for malformed/forged tokens."""
    head, sep, mac = token.partition(".")
    if not sep:
        return None
    try:
        canonical = uuid.UUID(head).hex
    except ValueError:
        return None
    expected = hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()[:MAC_LEN]
    if not hmac.compare_digest(expected, mac):
        return None
    return str(uuid.UUID(canonical))


def record_conversion(conn: Connection, touch_id: str) -> bool:
    """Attribute one conversion to a touch. Idempotent: repeat tokens no-op.

    Returns True if this call converted the prospect.
    """
    row = conn.execute(
        text(
            """
            SELECT p.id, p.state, t.slug, tc.variant_id
            FROM touches tc
            JOIN prospects p ON p.id = tc.prospect_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE tc.id = CAST(:i AS uuid)
            """
        ),
        {"i": touch_id},
    ).fetchone()
    if row is None:
        return False
    prospect_id, state, tenant, variant_id = row
    if state == ProspectState.CONVERTED:
        return False  # replayed token: idempotent no-op
    try:
        transition(conn, str(prospect_id), ProspectState.CONVERTED,
                   actor="system:attribution", reason=f"touch {touch_id}")
    except TransitionError:
        return False  # e.g. unsubscribed-then-clicked: conversion doesn't undo an exit
    if variant_id:
        record_success(conn, tenant, variant_id)  # conversions are the real metric
    # FR-4.4: conversion is a configured positive event — eligible personas may
    # make their one referral ask (the handler re-checks eligibility + flag).
    from open_reachout.core import queue

    queue.enqueue(conn, "referral", {"prospect_id": str(prospect_id)},
                  idempotency_key=f"referral:{prospect_id}")
    conn.execute(
        text(
            """
            INSERT INTO audit_events (subject_type, subject_id, event, payload, actor)
            VALUES ('touch', :i, 'converted', '{}'::jsonb, 'system:attribution')
            """
        ),
        {"i": touch_id},
    )
    return True
