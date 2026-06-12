"""Versioned claim registry (PRD FR-3.2 allowlist mode, spec 13.5).

Config (`about_us.approved_claims`) is the source of truth — versionable,
diffable, reviewed like any config change. This module mirrors it into the
`claim_registry` table so every historical version is an audited record, and
computes the version string the gatekeeper stamps on every send (FR-8.5).
Flipping a tenant to allowlist mode is config, not migration.
"""

from __future__ import annotations

import hashlib
import re

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core.config import AboutUs

DENYLIST_VERSION = "deny-pack@1"


def registry_version(about_us: AboutUs) -> str:
    """Deterministic version of the active claims posture."""
    if about_us.claims_mode != "allowlist":
        return DENYLIST_VERSION
    digest = hashlib.sha256(
        "\x00".join(sorted(c.strip().lower() for c in about_us.approved_claims)).encode()
    ).hexdigest()[:12]
    return f"allowlist@{digest}"


def _claim_id(claim_text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", claim_text.strip().lower()).strip("_")
    return slug[:60] or "claim"


def ensure_registry(conn: Connection, tenant: str, about_us: AboutUs) -> str:
    """Sync the audited registry to config; returns the active version string.
    Rows are append-only per (claim, version): a changed claim set is a new
    version, never an edit of history."""
    version = registry_version(about_us)
    if about_us.claims_mode == "allowlist":
        for claim_text in about_us.approved_claims:
            conn.execute(
                text(
                    """
                    INSERT INTO claim_registry (tenant, claim_id, version, claim_text)
                    VALUES (:t, :c, :v, :x)
                    ON CONFLICT (tenant, claim_id, version) DO NOTHING
                    """
                ),
                {"t": tenant, "c": _claim_id(claim_text), "v": version,
                 "x": claim_text.strip()},
            )
    return version
