"""Research notes at every level of granularity (cohort -> strategy ->
outreach).

The system researches before it acts at each layer: cohort-level notes inform
which segments to pursue, strategy-level notes inform which prompts to test,
and prospect-level Evidence Cards inform each individual email. The v0
researcher is deterministic — it synthesizes the deployment's OWN outcome and
evidence data (always true, always free); an LLM, when provided, adds a
narrative interpretation on top. Live web research plugs in via the same
note format once a search-capable backend is configured.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core.interfaces import LLMBackend
from open_reachout.core.metrics import funnel, strategies


class ResearchNarrative(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    recommendations: list[str] = []
    injection_suspected: bool = False


@dataclass(frozen=True)
class Note:
    level: str
    subject_id: str
    summary: str
    findings: dict
    created_at: datetime | None = None


def _store(conn: Connection, tenant: str, note: Note) -> None:
    conn.execute(
        text(
            """
            INSERT INTO research_notes (tenant, level, subject_id, summary, findings)
            VALUES (:t, :l, :s, :su, CAST(:f AS jsonb))
            """
        ),
        {"t": tenant, "l": note.level, "s": note.subject_id, "su": note.summary,
         "f": json.dumps(note.findings)},
    )


def latest(conn: Connection, tenant: str, level: str, subject_id: str) -> Note | None:
    row = conn.execute(
        text(
            """
            SELECT summary, findings, created_at FROM research_notes
            WHERE tenant = :t AND level = :l AND subject_id = :s
            ORDER BY created_at DESC LIMIT 1
            """
        ),
        {"t": tenant, "l": level, "s": subject_id},
    ).fetchone()
    return None if row is None else Note(level, subject_id, row[0], row[1] or {}, row[2])


def _evidence_themes(conn: Connection, tenant: str, cohort_id: str) -> dict[str, int]:
    rows = conn.execute(
        text(
            """
            SELECT ef.fact_type, count(*) FROM evidence_facts ef
            JOIN prospects p ON p.id = ef.prospect_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = :t AND p.cohort_id = :c GROUP BY 1 ORDER BY 2 DESC
            """
        ),
        {"t": tenant, "c": cohort_id},
    ).fetchall()
    return dict(rows)


def _reply_intents(conn: Connection, tenant: str, cohort_id: str) -> dict[str, int]:
    rows = conn.execute(
        text(
            """
            SELECT coalesce(r.intent, 'unclassified'), count(*) FROM replies r
            JOIN prospects p ON p.id = r.prospect_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = :t AND p.cohort_id = :c GROUP BY 1 ORDER BY 2 DESC
            """
        ),
        {"t": tenant, "c": cohort_id},
    ).fetchall()
    return dict(rows)


def refresh_cohort_note(
    conn: Connection, tenant: str, cohort_id: str, llm: LLMBackend | None = None
) -> Note:
    """Synthesize what the deployment knows about a cohort (informs cohorts)."""
    f = funnel(conn, tenant, cohort_id)
    themes = _evidence_themes(conn, tenant, cohort_id)
    intents = _reply_intents(conn, tenant, cohort_id)
    findings = {
        "reached": dict(f.reached), "exits": f.exits, "replies": f.replies,
        "positive_replies": f.positive_replies, "converted": f.converted,
        "evidence_themes": themes, "reply_intents": intents,
    }
    parts = [
        f"{f.contacted} contacted; {f.replies} replied"
        + (f" ({f.reply_rate:.0%})" if f.contacted else "")
        + f"; {f.converted} converted"
        + (f" ({f.conversion_rate:.0%})." if f.contacted else "."),
    ]
    if f.exits:
        top_exit = max(f.exits.items(), key=lambda kv: kv[1])
        parts.append(f"Biggest drop-off: {top_exit[0]} ({top_exit[1]}).")
    if intents:
        parts.append(
            "Reply intents: " + ", ".join(f"{k} {v}" for k, v in intents.items()) + "."
        )
    if themes:
        parts.append(
            "Evidence coverage: " + ", ".join(f"{k} {v}" for k, v in themes.items()) + "."
        )
    summary = " ".join(parts)
    if llm is not None:
        narrative = llm.complete(
            "discovery_research",
            "Interpret this cohort's outreach data for the operator. Be concrete "
            "about what is working, what is failing, and what to try next. Do not "
            f"invent numbers.\n\nData:\n{json.dumps(findings, indent=2)}",
            ResearchNarrative,
        )
        assert isinstance(narrative, ResearchNarrative)
        if not narrative.injection_suspected:
            summary = narrative.summary + " | " + summary
            findings["recommendations"] = narrative.recommendations
    note = Note("cohort", cohort_id, summary, findings)
    _store(conn, tenant, note)
    return note


def refresh_strategy_notes(
    conn: Connection, tenant: str, llm: LLMBackend | None = None
) -> list[Note]:
    """One note per strategy/variant (informs the strategies being tested)."""
    notes: list[Note] = []
    for s in strategies(conn, tenant):
        findings = {
            "trials": s.trials, "successes": s.successes, "success_rate": s.success_rate,
            "bounces": s.bounces, "complaints": s.complaints,
            "attributes": s.attributes, "paused": s.paused,
        }
        summary = (
            f"{s.trials} sends, {s.successes} positive"
            + (f" ({s.success_rate:.0%})" if s.trials else "")
            + (f"; attributes: {', '.join(f'{k}={v}' for k, v in s.attributes.items())}"
               if s.attributes else "")
            + ("; PAUSED by guardrail" if s.paused else "")
            + "."
        )
        note = Note("strategy", s.variant_id, summary, findings)
        _store(conn, tenant, note)
        notes.append(note)
    return notes


def refresh_campaign_note(
    conn: Connection, tenant: str, llm: LLMBackend | None = None,
    research_directive: str = "",
) -> Note:
    """Campaign/market tier (FR-2.11, spec 8.10): tenant-wide dynamics, mined
    BEFORE cohort work so market research flows into cohort design. The
    deterministic core aggregates the deployment's own cross-cohort outcomes;
    an LLM narrative interprets them against the Brief's `research` directive."""
    rows = conn.execute(
        text(
            """
            SELECT p.cohort_id,
                   count(*) FILTER (WHERE p.state NOT IN ('discovered','enriched',
                       'qualified','queued')) AS reached,
                   count(*) FILTER (WHERE p.state = 'converted') AS converted
            FROM prospects p JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = :t GROUP BY 1 ORDER BY 2 DESC
            """
        ),
        {"t": tenant},
    ).fetchall()
    intents = conn.execute(
        text(
            """
            SELECT coalesce(r.intent, 'unclassified'), count(*) FROM replies r
            JOIN prospects p ON p.id = r.prospect_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = :t GROUP BY 1 ORDER BY 2 DESC
            """
        ),
        {"t": tenant},
    ).fetchall()
    findings: dict[str, object] = {
        "cohorts": {r[0]: {"reached": r[1], "converted": r[2]} for r in rows},
        "reply_intents": {str(k): int(v) for k, v in intents},
        "research_directive": research_directive,
    }
    total_reached = sum(r[1] for r in rows)
    total_converted = sum(r[2] for r in rows)
    summary = (
        f"Market view across {len(rows)} cohort(s): {total_reached} reached, "
        f"{total_converted} converted."
    )
    if rows:
        best = max(rows, key=lambda r: (r[2] / r[1]) if r[1] else 0.0)
        summary += f" Strongest cohort so far: {best[0]}."
    if llm is not None:
        narrative = llm.complete(
            "discovery_research",
            "Interpret this tenant's cross-cohort outreach data as MARKET "
            "research, guided by the operator's research directive below. What "
            "market dynamics do the numbers suggest? Which kinds of prospects "
            "respond? Do not invent numbers.\n\n"
            f"Research directive: {research_directive or '(none)'}\n\n"
            f"Data:\n{json.dumps(findings, indent=2)}",
            ResearchNarrative,
        )
        assert isinstance(narrative, ResearchNarrative)
        if not narrative.injection_suspected:
            summary = narrative.summary + " | " + summary
            findings["recommendations"] = narrative.recommendations
    note = Note("campaign", tenant, summary, findings)
    _store(conn, tenant, note)
    return note


def refresh_all(
    conn: Connection, tenant: str, llm: LLMBackend | None = None,
    research_directive: str = "",
) -> int:
    cohort_ids = [
        r[0]
        for r in conn.execute(
            text(
                """
                SELECT DISTINCT p.cohort_id FROM prospects p
                JOIN tenants t ON t.id = p.tenant_id WHERE t.slug = :t
                """
            ),
            {"t": tenant},
        ).fetchall()
    ]
    # Campaign/market tier first: market research flows into cohort design.
    refresh_campaign_note(conn, tenant, llm, research_directive=research_directive)
    n = 1
    for cohort_id in cohort_ids:
        refresh_cohort_note(conn, tenant, cohort_id, llm)
        n += 1
    n += len(refresh_strategy_notes(conn, tenant, llm))
    return n
