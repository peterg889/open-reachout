"""Bandit persistence (spec 8.6, FR-5.1/5.2): the learning loop's storage.

Observations arrive from the event flow (dispatch -> trial, positive reply ->
success, bounce/complaint/unsub -> guardrail counters with deterministic
auto-pause). Selection loads arms and runs Thompson sampling at compose time.
"""

from __future__ import annotations

import json
import random

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core.config import VariantSpec
from open_reachout.stats.bandit import VariantArm, guardrail_breaches, select

GUARDRAIL_COLUMNS = {"bounce": "bounces", "complaint": "complaints", "unsubscribe": "unsubs"}


def _upsert(conn: Connection, tenant: str, variant_id: str, column: str) -> None:
    conn.execute(
        text(
            f"""
            INSERT INTO variant_stats (tenant, variant_id, {column})
            VALUES (:t, :v, 1)
            ON CONFLICT (tenant, variant_id)
            DO UPDATE SET {column} = variant_stats.{column} + 1
            """  # noqa: S608 — column from a closed internal map
        ),
        {"t": tenant, "v": variant_id},
    )


def record_trial(
    conn: Connection, tenant: str, variant_id: str, attributes: dict[str, str] | None = None
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO variant_stats (tenant, variant_id, trials, attributes)
            VALUES (:t, :v, 1, CAST(:a AS jsonb))
            ON CONFLICT (tenant, variant_id)
            DO UPDATE SET trials = variant_stats.trials + 1
            """
        ),
        {"t": tenant, "v": variant_id, "a": json.dumps(attributes or {})},
    )


def record_success(conn: Connection, tenant: str, variant_id: str) -> None:
    _upsert(conn, tenant, variant_id, "successes")


def record_guardrail(conn: Connection, tenant: str, variant_id: str, kind: str) -> bool:
    """Increment a guardrail counter and auto-pause on breach (FR-5.2).

    Deterministic — runs inside event handlers, never waits on a model.
    Returns True when the variant was paused by this observation.
    """
    _upsert(conn, tenant, variant_id, GUARDRAIL_COLUMNS[kind])
    arm = _load_one(conn, tenant, variant_id)
    if arm is None or arm.paused:
        return False
    breaches = guardrail_breaches(arm)
    if not breaches:
        return False
    conn.execute(
        text(
            """
            UPDATE variant_stats SET paused = true
            WHERE tenant = :t AND variant_id = :v
            """
        ),
        {"t": tenant, "v": variant_id},
    )
    conn.execute(
        text(
            """
            INSERT INTO audit_events (subject_type, subject_id, event, payload, actor)
            VALUES ('variant', :v, 'guardrail_paused', CAST(:pl AS jsonb), 'system:learning')
            """
        ),
        {"v": variant_id, "pl": json.dumps({"tenant": tenant, "breaches": breaches})},
    )
    return True


def _row_to_arm(row: tuple) -> VariantArm:
    variant_id, attributes, trials, successes, complaints, unsubs, bounces, paused = row
    return VariantArm(
        variant_id=variant_id, trials=trials, successes=successes,
        attributes=attributes or {}, paused=paused,
        complaints=complaints, unsubs=unsubs, bounces=bounces,
    )


_ARM_COLUMNS = "variant_id, attributes, trials, successes, complaints, unsubs, bounces, paused"


def _load_one(conn: Connection, tenant: str, variant_id: str) -> VariantArm | None:
    row = conn.execute(
        text(
            f"SELECT {_ARM_COLUMNS} FROM variant_stats WHERE tenant = :t AND variant_id = :v"
        ),
        {"t": tenant, "v": variant_id},
    ).fetchone()
    return None if row is None else _row_to_arm(row)


def load_arms(conn: Connection, tenant: str, variants: list[VariantSpec]) -> list[VariantArm]:
    """Arms for the given variant specs; unseen variants start cold (Beta prior)."""
    rows = conn.execute(
        text(
            f"""
            SELECT {_ARM_COLUMNS} FROM variant_stats
            WHERE tenant = :t AND variant_id = ANY(:ids)
            """
        ),
        {"t": tenant, "ids": [v.id for v in variants]},
    ).fetchall()
    by_id = {r[0]: _row_to_arm(r) for r in rows}
    return [
        by_id.get(v.id, VariantArm(variant_id=v.id, attributes=v.attributes)) for v in variants
    ]


def global_success_rate(conn: Connection, tenant: str, floor: float = 0.05) -> float:
    """Aggregate observed positive-reply rate, floored for cold deployments."""
    row = conn.execute(
        text(
            """
            SELECT COALESCE(SUM(successes), 0), COALESCE(SUM(trials), 0)
            FROM variant_stats WHERE tenant = :t
            """
        ),
        {"t": tenant},
    ).fetchone()
    successes, trials = row or (0, 0)
    if trials < 50:  # too little signal: planning baseline (research report)
        return floor
    return max(successes / trials, 0.005)


def select_variant(
    conn: Connection, tenant: str, variants: list[VariantSpec], rng: random.Random | None = None
) -> tuple[VariantSpec, dict[str, float]]:
    """Thompson-select among a persona's variants (compose-time entry point).

    Returns the chosen spec plus the posterior snapshot for the decision trace
    (FR-8.5). Attribute-effect pooling lands with the nightly recompute job;
    v0 passes empty effects (spec 10.2 upgrade path).
    """
    arms = load_arms(conn, tenant, variants)
    rate = global_success_rate(conn, tenant)
    chosen = select(arms, {}, rate, rng or random.Random())
    spec = next(v for v in variants if v.id == chosen.variant_id)
    alpha, beta = chosen.posterior(rate)
    return spec, {"alpha": alpha, "beta": beta, "global_rate": rate}
