"""Campaign-level sentiment auto-throttle (PRD FR-5.6, spec 10.3).

Souring is visible in replies before it shows up in complaint rates; the
framework reacts at the earlier signal. A rolling EWMA over classified reply
intents per cohort: mildly negative halves the cohort's monthly cap (audited
counter rewrite, once per period); strongly negative zeroes it and escalates.
Recovery is an operator action — restore the cap deliberately (the escalation
records the prior value); the throttle never un-throttles itself.

Deterministic end to end: scoring is a lookup, not a model call (I-11 — the
brake must work when models are down).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core.escalations import escalate

#: Spec 10.3 scoring. Unlisted intents (question, out_of_office, wrong_person,
#: other) are neutral. Complaints arrive via provider events, not replies, and
#: are covered by the kill switches (FR-7.4) — this throttle reads replies.
SCORES: dict[str, float] = {
    "interested": 2.0,
    "objection": -0.5,
    "not_interested": -1.0,
    "unsubscribe": -2.0,
    "hostile": -3.0,
}
HALF_LIFE_REPLIES = 20
ALPHA = 1 - 0.5 ** (1 / HALF_LIFE_REPLIES)
#: Don't act on noise: minimum classified replies before the score is trusted.
MIN_REPLIES = 10
THROTTLE_BELOW = -0.5
PAUSE_BELOW = -1.2
#: Evaluate on every Nth classified reply (plus any nightly sweep).
EVALUATE_EVERY = 10
#: Bounded read: the EWMA half-life makes older replies negligible anyway.
WINDOW = 200


def cohort_sentiment(conn: Connection, tenant: str, cohort_id: str) -> tuple[float, int]:
    """EWMA sentiment over the cohort's classified replies, oldest→newest."""
    rows = conn.execute(
        text(
            """
            SELECT r.intent FROM replies r
            JOIN prospects p ON p.id = r.prospect_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = :t AND p.cohort_id = :c AND r.intent IS NOT NULL
            ORDER BY r.received_at DESC LIMIT :w
            """
        ),
        {"t": tenant, "c": cohort_id, "w": WINDOW},
    ).fetchall()
    intents = [r[0] for r in reversed(rows)]
    score = 0.0
    for intent in intents:
        score += ALPHA * (SCORES.get(intent, 0.0) - score)
    return score, len(intents)


def _already_acted(conn: Connection, cohort_id: str, event: str, period: str) -> bool:
    return (
        conn.execute(
            text(
                """
                SELECT 1 FROM audit_events
                WHERE subject_type = 'cohort' AND subject_id = :c AND event = :e
                  AND payload->>'period' = :p
                LIMIT 1
                """
            ),
            {"c": cohort_id, "e": event, "p": period},
        ).fetchone()
        is not None
    )


def _audit(
    conn: Connection, cohort_id: str, event: str, payload: dict[str, object]
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO audit_events (subject_type, subject_id, event, payload, actor)
            VALUES ('cohort', :c, :e, CAST(:pl AS jsonb), 'system:sentiment')
            """
        ),
        {"c": cohort_id, "e": event, "pl": json.dumps(payload)},
    )


def evaluate_cohort(conn: Connection, tenant: str, cohort_id: str) -> str | None:
    """Apply throttle/pause if the cohort's sentiment warrants it. Returns the
    action taken ('throttled' | 'paused') or None. Idempotent per period."""
    score, n = cohort_sentiment(conn, tenant, cohort_id)
    if n < MIN_REPLIES:
        return None
    period = datetime.now(UTC).strftime("%Y-%m")
    if score < PAUSE_BELOW:
        if _already_acted(conn, cohort_id, "sentiment_pause", period):
            return None
        prior = conn.execute(
            text(
                """
                UPDATE counters SET cap = 0
                WHERE scope_type = 'cohort_month' AND scope_id = :c AND period = :p
                RETURNING (SELECT cap FROM counters
                           WHERE scope_type='cohort_month' AND scope_id=:c AND period=:p)
                """
            ),
            {"c": cohort_id, "p": period},
        ).scalar()
        _audit(conn, cohort_id, "sentiment_pause",
               {"period": period, "score": round(score, 3), "replies": n,
                "prior_cap": prior})
        escalate(
            conn, tenant=tenant, subject_type="cohort", subject_id=cohort_id,
            reason=f"sentiment pause: score {score:.2f} over {n} replies — cap zeroed; "
                   f"restore deliberately (prior cap {prior})",
            payload={"score": round(score, 3), "prior_cap": prior},
        )
        return "paused"
    if score < THROTTLE_BELOW:
        if _already_acted(conn, cohort_id, "sentiment_throttle", period):
            return None
        conn.execute(
            text(
                """
                UPDATE counters SET cap = GREATEST(cap / 2, 1)
                WHERE scope_type = 'cohort_month' AND scope_id = :c AND period = :p
                """
            ),
            {"c": cohort_id, "p": period},
        )
        _audit(conn, cohort_id, "sentiment_throttle",
               {"period": period, "score": round(score, 3), "replies": n})
        return "throttled"
    return None


def maybe_evaluate(conn: Connection, tenant: str, cohort_id: str) -> str | None:
    """Classify-path hook (spec 10.3): evaluate on every Nth classified reply."""
    n = conn.execute(
        text(
            """
            SELECT count(*) FROM replies r
            JOIN prospects p ON p.id = r.prospect_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = :t AND p.cohort_id = :c AND r.intent IS NOT NULL
            """
        ),
        {"t": tenant, "c": cohort_id},
    ).scalar()
    if n and n % EVALUATE_EVERY == 0:
        return evaluate_cohort(conn, tenant, cohort_id)
    return None
