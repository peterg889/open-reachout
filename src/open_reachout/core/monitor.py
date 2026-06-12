"""Production hallucination monitoring (PRD FR-8.6, spec 7.3/14).

The gate-1 groundedness check is a release-time property; this module makes it
a *production* property: a sampled audit over already-SENT mail, re-judging
each message against the prospect's stored Evidence Card. The grounded rate
per cohort is recorded as an audit event (the digest/metrics read it), and a
rate below the alert threshold escalates with the offending touches attached.

Spend posture: this is a metered, low-frequency research-budget task — the
sample size bounds the cost, and a failure here never blocks sending (it
alerts humans; the gatekeeper already enforced gate 1 pre-send).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.agents.prompts import GROUNDEDNESS_FRAME
from open_reachout.agents.schemas import GroundednessOutput
from open_reachout.core.escalations import escalate
from open_reachout.core.interfaces import LLMBackend
from open_reachout.security.envelope import wrap

PER_COHORT_SAMPLE = 5
WINDOW_DAYS = 7
ALERT_BELOW = 0.95


def _evidence_blocks(conn: Connection, prospect_id: str) -> str:
    rows = conn.execute(
        text(
            """
            SELECT id, content FROM evidence_facts
            WHERE prospect_id = CAST(:p AS uuid) ORDER BY observed_at DESC LIMIT 12
            """
        ),
        {"p": prospect_id},
    ).fetchall()
    blocks = [
        f"fact_id={fact_id}\n"
        + wrap(content if isinstance(content, str) else json.dumps(content),
               source="web", idem=str(fact_id)).text
        for fact_id, content in rows
    ]
    return "\n\n".join(blocks) or "(no evidence on file)"


def audit_sent_sample(
    conn: Connection,
    llm: LLMBackend,
    *,
    per_cohort: int = PER_COHORT_SAMPLE,
    window_days: int = WINDOW_DAYS,
    alert_below: float = ALERT_BELOW,
) -> dict[str, dict[str, float | int]]:
    """Re-judge a random sample of sent mail per (tenant, cohort). Returns
    {"tenant/cohort": {rate, n, failures}} and records/escalates as it goes."""
    rows = conn.execute(
        text(
            """
            SELECT t.slug, p.cohort_id, tc.id, tc.subject, tc.body, p.id
            FROM touches tc
            JOIN prospects p ON p.id = tc.prospect_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE tc.status IN ('dispatched', 'sent', 'delivered')
              AND tc.body IS NOT NULL AND NOT tc.scrubbed
              AND tc.sent_at > now() - make_interval(days => :w)
            ORDER BY random()
            """
        ),
        {"w": window_days},
    ).fetchall()
    by_cohort: dict[tuple[str, str], list[tuple[str, str, str, str]]] = {}
    for slug, cohort, touch_id, subject, body, prospect_id in rows:
        bucket = by_cohort.setdefault((slug, cohort), [])
        if len(bucket) < per_cohort:
            bucket.append((str(touch_id), subject or "", body or "", str(prospect_id)))

    period = datetime.now(UTC).strftime("%G-W%V")  # ISO week
    results: dict[str, dict[str, float | int]] = {}
    for (slug, cohort), sample in by_cohort.items():
        failures: list[dict[str, object]] = []
        for touch_id, subject, body, prospect_id in sample:
            prompt = GROUNDEDNESS_FRAME.format(
                subject=subject, body=body,
                claims="(unavailable post-send; audit every prospect-specific statement)",
                evidence_blocks=_evidence_blocks(conn, prospect_id),
            )
            verdict = llm.complete("groundedness", prompt, GroundednessOutput)
            assert isinstance(verdict, GroundednessOutput)
            if not verdict.grounded or verdict.injection_suspected:
                failures.append(
                    {"touch_id": touch_id,
                     "unsupported": verdict.unsupported_claims[:5]}
                )
        rate = 1.0 - (len(failures) / len(sample)) if sample else 1.0
        results[f"{slug}/{cohort}"] = {"rate": round(rate, 3), "n": len(sample),
                                       "failures": len(failures)}
        conn.execute(
            text(
                """
                INSERT INTO audit_events (subject_type, subject_id, event, payload, actor)
                VALUES ('cohort', :c, 'groundedness_audit', CAST(:pl AS jsonb),
                        'system:monitor')
                """
            ),
            {"c": cohort,
             "pl": json.dumps({"period": period, "tenant": slug,
                               "rate": round(rate, 3), "n": len(sample),
                               "failures": failures})},
        )
        if rate < alert_below:
            escalate(
                conn, tenant=slug, subject_type="cohort", subject_id=cohort,
                reason=(
                    f"groundedness rate {rate:.0%} over {len(sample)} sampled sends "
                    f"(< {alert_below:.0%}) — review the attached touches (FR-8.6)"
                ),
                payload={"period": period, "failures": failures},
            )
    return results
