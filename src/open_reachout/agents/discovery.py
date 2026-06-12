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
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core import proposals
from open_reachout.core.config import TenantConfig
from open_reachout.core.interfaces import LLMBackend

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


# --------------------------------------------------------- goal brainstorming
class BrainstormIdea(BaseModel):
    """One proposed objective (FR-0.5). Ideas are directions for a human to
    approve, never auto-applied — `goal_brainstorm` is outside AUTO_APPLICABLE."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["value_prop_angle", "adjacent_audience", "seasonal_push", "partnership"]
    slug: str = Field(pattern=r"^[a-z0-9_]+$", description="stable identity for dedupe")
    summary: str = Field(min_length=10, max_length=300)
    rationale: str = Field(min_length=10)
    program_delta: str = Field(min_length=10, description="concrete next step in config terms")


class BrainstormResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ideas: list[BrainstormIdea]


BRAINSTORM_FRAME = """You work the `brainstorm` directive of an outreach Brief:
propose NEW OBJECTIVES worth pursuing — value-prop angles to test, adjacent
audiences, seasonal pushes, partnership/channel ideas. A human reviews every
idea; nothing you output launches anything.

Rules:
- Ground every idea in the outcome data below; cite it in `rationale`.
- Never invent product facts: the only product truths are in about_us below.
- `program_delta` must be a concrete, config-shaped next step (a cohort to
  add, a variant attribute to test, a budget to shift toward a season).
- At most {max_ideas} ideas; fewer, better-grounded ideas beat filler.

Brainstorm directive: {directive}
Conversion goal: {convert}
about_us: {about_us}

Cohort outcomes (contacted/engaged/converted):
{outcomes}

Reply-intent mix (what the market is saying back):
{intents}
"""


def _intent_mix(conn: Connection, tenant: str) -> str:
    rows = conn.execute(
        text(
            """
            SELECT r.intent, count(*) FROM replies r
            JOIN prospects p ON p.id = r.prospect_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = :t AND r.intent IS NOT NULL
            GROUP BY r.intent ORDER BY count(*) DESC
            """
        ),
        {"t": tenant},
    ).fetchall()
    return "\n".join(f"- {intent}: {n}" for intent, n in rows) or "- (no replies yet)"


def brainstorm(
    conn: Connection, llm: LLMBackend, config: TenantConfig, *, max_ideas: int = 5
) -> list[str]:
    """Work the Brief's `brainstorm` directive (FR-0.5): propose new objectives
    as Proposals with evidence. Returns recorded proposal ids; declined
    directions are remembered by the proposal store and not re-pitched."""
    directive = config.brief.goals.brainstorm
    if not directive:
        return []
    outcomes = cohort_outcomes(conn, config.tenant)
    outcome_lines = "\n".join(
        f"- {o.cohort_id}: {o.contacted} contacted, {o.engaged} engaged, "
        f"{o.converted} converted ({o.conversion_rate:.1%})"
        for o in outcomes
    ) or "- (no outcome data yet)"
    prompt = BRAINSTORM_FRAME.format(
        max_ideas=max_ideas,
        directive=directive,
        convert=config.brief.goals.convert,
        about_us=config.brief.about_us.what_we_do.strip(),
        outcomes=outcome_lines,
        intents=_intent_mix(conn, config.tenant),
    )
    result = llm.complete("brainstorm_goals", prompt, BrainstormResult)
    assert isinstance(result, BrainstormResult)
    recorded: list[str] = []
    for idea in result.ideas[:max_ideas]:
        pid = proposals.propose(
            conn,
            tenant=config.tenant,
            kind="goal_brainstorm",
            summary=f"[{idea.kind}] {idea.summary}",
            payload={"kind": idea.kind, "program_delta": idea.program_delta},
            evidence={"rationale": idea.rationale},
            dedupe_key=f"brainstorm:{idea.slug}",
        )
        if pid:
            recorded.append(pid)
    return recorded


# ----------------------------------------------------------- rebalancing (FR-6.5)
#: One-sided confidence for the Wilson upper bound (z for 90%).
_WILSON_Z = 1.2816


def wilson_upper(successes: int, trials: int, z: float = _WILSON_Z) -> float:
    """One-sided upper confidence bound on a rate — flags only sustained,
    statistically-supported underperformance (spec 8.11)."""
    if trials == 0:
        return 1.0
    import math

    p = successes / trials
    denom = 1 + z * z / trials
    centre = p + z * z / (2 * trials)
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return min(1.0, (centre + margin) / denom)


def _reply_counts(conn: Connection, tenant: str) -> dict[str, int]:
    rows = conn.execute(
        text(
            """
            SELECT p.cohort_id, count(*) FROM replies r
            JOIN prospects p ON p.id = r.prospect_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = :t GROUP BY 1
            """
        ),
        {"t": tenant},
    ).fetchall()
    return {str(k): int(v) for k, v in rows}


def rebalance_scan(conn: Connection, config: TenantConfig) -> list[str]:
    """FR-6.5: flag cohorts whose realized rates sit below their configured
    floors with statistical support; emit `rebalance` Proposals (pause, or
    shift toward the strongest cohort). Deterministic and free — runs with
    every `reachout discover`."""
    outcomes = {o.cohort_id: o for o in cohort_outcomes(conn, config.tenant)}
    replies = _reply_counts(conn, config.tenant)
    best = max(outcomes.values(), key=lambda o: o.conversion_rate, default=None)
    recorded: list[str] = []
    for persona in config.personas:
        for cohort in persona.cohorts:
            floors = cohort.floors
            outcome = outcomes.get(cohort.id)
            if floors is None or outcome is None:
                continue
            if outcome.contacted < floors.min_trials:
                continue
            breaches: list[str] = []
            if floors.conversion_rate is not None:
                upper = wilson_upper(outcome.converted, outcome.contacted)
                if upper < floors.conversion_rate:
                    breaches.append(
                        f"conversion upper bound {upper:.1%} < floor "
                        f"{floors.conversion_rate:.1%} ({outcome.converted}/"
                        f"{outcome.contacted})"
                    )
            if floors.reply_rate is not None:
                n_replies = replies.get(cohort.id, 0)
                upper = wilson_upper(n_replies, outcome.contacted)
                if upper < floors.reply_rate:
                    breaches.append(
                        f"reply upper bound {upper:.1%} < floor "
                        f"{floors.reply_rate:.1%} ({n_replies}/{outcome.contacted})"
                    )
            if not breaches:
                continue
            shift_target = (
                best.cohort_id
                if best is not None and best.cohort_id != cohort.id
                and best.conversion_rate > outcome.conversion_rate
                else None
            )
            cap = _cohort_cap(conn, cohort.id)
            action: dict[str, object] = (
                {"action": "shift", "from_cohort": cohort.id,
                 "to_cohort": shift_target, "amount": max(cap // 2, 1)}
                if shift_target
                else {"action": "pause", "cohort": cohort.id}
            )
            pid = proposals.propose(
                conn,
                tenant=config.tenant,
                kind="rebalance",
                summary=f"Rebalance {cohort.id}: " + "; ".join(breaches),
                payload=action,
                evidence={"breaches": breaches, "contacted": outcome.contacted,
                          "converted": outcome.converted},
                dedupe_key=f"rebalance:{cohort.id}",
            )
            if pid:
                recorded.append(pid)
    return recorded


# ------------------------------------------------- re-synthesis on drift (FR-0.6)
#: Minimum classified replies before negative share counts as drift.
MIN_REPLIES_FOR_DRIFT = 20
#: Negative-intent share of replies that contradicts the program's value props.
NEGATIVE_SHARE_THRESHOLD = 0.5
NEGATIVE_INTENTS = ("not_interested", "unsubscribe", "hostile")


def detect_drift(conn: Connection, tenant: str) -> list[str]:
    """Outcome signals diverging from the program's assumptions. Deterministic
    and cheap — the LLM is only consulted once drift is established."""
    signals: list[str] = []
    for o in cohort_outcomes(conn, tenant):
        if o.contacted >= 2 * MIN_SIGNAL and o.converted == 0:
            signals.append(
                f"cohort {o.cohort_id}: {o.contacted} contacted, 0 conversions"
            )
    row = conn.execute(
        text(
            """
            SELECT count(*) FILTER (WHERE r.intent = ANY(:neg)) AS negative,
                   count(*) AS total
            FROM replies r
            JOIN prospects p ON p.id = r.prospect_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = :t AND r.intent IS NOT NULL
            """
        ),
        {"t": tenant, "neg": list(NEGATIVE_INTENTS)},
    ).fetchone()
    if row and row[1] >= MIN_REPLIES_FOR_DRIFT:
        share = row[0] / row[1]
        if share >= NEGATIVE_SHARE_THRESHOLD:
            signals.append(
                f"negative reply share {share:.0%} ({row[0]}/{row[1]}) — replies "
                "contradict the program's value props"
            )
    return signals


def resynthesize_on_drift(
    conn: Connection, llm: LLMBackend, config: TenantConfig
) -> str | None:
    """FR-0.6: when outcomes diverge from program assumptions, run synthesis in
    revision mode and file the revised program as a `program_revision` Proposal
    (always-human — a revision is a value-prop-level change). Returns the
    proposal id, or None when there is no drift or an open/declined duplicate."""
    from open_reachout.agents import synthesizer

    signals = detect_drift(conn, config.tenant)
    if not signals:
        return None
    from open_reachout.core import research

    campaign_note = research.latest(conn, config.tenant, "campaign", config.tenant)
    revised = synthesizer.revise(
        llm, config, evidence="\n".join(f"- {s}" for s in signals),
        market_research=campaign_note.summary if campaign_note else None,
    )
    assert revised.generated_by is not None  # revise() always stamps provenance
    return proposals.propose(
        conn,
        tenant=config.tenant,
        kind="program_revision",
        summary=f"Program revision addressing drift: {'; '.join(signals)[:200]}",
        payload={
            "personas": [p.model_dump(mode="json") for p in revised.personas],
            "generated_by": revised.generated_by.model_dump(mode="json"),
        },
        evidence={"signals": signals},
        dedupe_key=f"program_revision:{revised.generated_by.config_hash}",
    )
