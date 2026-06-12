"""The discovery agent (PRD FR-6.1, spec 8.7): outcome mining -> Proposals.

The account-free core analyzes the deployment's own outcome data and proposes
budget reallocation between existing cohorts and flags underperformers. The
LLM web-research path (proposing brand-new cohorts/value-props from the
Brief's `brainstorm` directives) is scaffolded but requires a live model +
search budget — it produces `new_cohort` proposals a human must turn into
config, never auto-applied.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core import proposals

#: Minimum contacted prospects before a cohort's rate is trusted as signal.
MIN_SIGNAL = 20
#: Propose a shift when the best cohort's conversion rate is this multiple of
#: the worst (and both clear MIN_SIGNAL).
SHIFT_RATIO = 2.0
#: Fraction of the loser's monthly cap to propose moving.
SHIFT_FRACTION = 0.25


@dataclass(frozen=True)
class CohortOutcome:
    cohort_id: str
    contacted: int
    engaged: int
    converted: int

    @property
    def conversion_rate(self) -> float:
        return self.converted / self.contacted if self.contacted else 0.0


def cohort_outcomes(conn: Connection, tenant: str) -> list[CohortOutcome]:
    rows = conn.execute(
        text(
            """
            SELECT p.cohort_id,
                   count(*) FILTER (WHERE p.state IN
                       ('contacted','engaged','converted','declined',
                        'unsubscribed','bounced','no_response')) AS contacted,
                   count(*) FILTER (WHERE p.state IN ('engaged','converted')) AS engaged,
                   count(*) FILTER (WHERE p.state = 'converted') AS converted
            FROM prospects p
            JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = :t
            GROUP BY p.cohort_id
            """
        ),
        {"t": tenant},
    ).fetchall()
    return [CohortOutcome(r[0], r[1], r[2], r[3]) for r in rows]


def _cohort_cap(conn: Connection, cohort_id: str) -> int:
    from datetime import UTC, datetime

    period = datetime.now(UTC).strftime("%Y-%m")
    return conn.execute(
        text(
            """
            SELECT cap FROM counters
            WHERE scope_type = 'cohort_month' AND scope_id = :s AND period = :p
            """
        ),
        {"s": cohort_id, "p": period},
    ).scalar() or 0


def analyze(conn: Connection, tenant: str) -> list[str]:
    """Run outcome mining for one tenant; emit Proposals. Returns proposal ids
    actually recorded (dedupe/decline-memory may suppress some)."""
    outcomes = [o for o in cohort_outcomes(conn, tenant) if o.contacted >= MIN_SIGNAL]
    recorded: list[str] = []
    if len(outcomes) < 2:
        return recorded  # need at least two comparable cohorts

    best = max(outcomes, key=lambda o: o.conversion_rate)
    worst = min(outcomes, key=lambda o: o.conversion_rate)
    if best.cohort_id == worst.cohort_id:
        return recorded

    if worst.conversion_rate == 0 or best.conversion_rate >= SHIFT_RATIO * worst.conversion_rate:
        worst_cap = _cohort_cap(conn, worst.cohort_id)
        amount = max(int(worst_cap * SHIFT_FRACTION), 1)
        pid = proposals.propose(
            conn,
            tenant=tenant,
            kind="budget_shift",
            summary=(
                f"Shift {amount}/mo from {worst.cohort_id} "
                f"({worst.conversion_rate:.1%} conv) to {best.cohort_id} "
                f"({best.conversion_rate:.1%} conv)"
            ),
            payload={"from_cohort": worst.cohort_id, "to_cohort": best.cohort_id,
                     "amount": amount},
            evidence={
                "best": {"cohort": best.cohort_id, "contacted": best.contacted,
                         "converted": best.converted},
                "worst": {"cohort": worst.cohort_id, "contacted": worst.contacted,
                          "converted": worst.converted},
            },
            dedupe_key=f"budget_shift:{worst.cohort_id}:{best.cohort_id}",
        )
        if pid:
            recorded.append(pid)

    # Flag a cohort that contacted MIN_SIGNAL+ with zero conversions as an
    # opportunity to revisit (not auto-applicable; a human looks).
    for o in outcomes:
        if o.converted == 0 and o.contacted >= 2 * MIN_SIGNAL:
            pid = proposals.propose(
                conn,
                tenant=tenant,
                kind="opportunity",
                summary=f"{o.cohort_id}: {o.contacted} contacted, 0 conversions — revisit "
                        "targeting, value prop, or pause",
                payload={"cohort": o.cohort_id},
                evidence={"contacted": o.contacted, "engaged": o.engaged},
                dedupe_key=f"zero_conv:{o.cohort_id}",
            )
            if pid:
                recorded.append(pid)
    return recorded
