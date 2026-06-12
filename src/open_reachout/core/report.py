"""The operator digest (PRD FR-8.1, `reachout report`): funnel, sends,
replies, variant leaderboard, escalations, suppression, queue health."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection


def _section(title: str, lines: list[str]) -> list[str]:
    return [f"## {title}", *(lines or ["(nothing)"]), ""]


def build_report(conn: Connection) -> str:
    out: list[str] = ["# Open Reachout digest", ""]

    funnel = conn.execute(
        text(
            """
            SELECT t.slug, p.state, count(*) FROM prospects p
            JOIN tenants t ON t.id = p.tenant_id
            GROUP BY t.slug, p.state ORDER BY t.slug, p.state
            """
        )
    ).fetchall()
    out += _section("Funnel", [f"- {t}: {s} = {n}" for t, s, n in funnel])

    counters = conn.execute(
        text(
            """
            SELECT scope_type, scope_id, period, used, cap FROM counters
            WHERE used > 0 ORDER BY scope_type, scope_id
            """
        )
    ).fetchall()
    out += _section(
        "Budgets", [f"- {st} {si} ({p}): {u}/{c}" for st, si, p, u, c in counters]
    )

    replies = conn.execute(
        text(
            """
            SELECT coalesce(intent, 'unclassified'), count(*) FROM replies
            GROUP BY 1 ORDER BY 2 DESC
            """
        )
    ).fetchall()
    out += _section("Replies by intent", [f"- {i}: {n}" for i, n in replies])

    objections = conn.execute(
        text(
            """
            SELECT tenant, class, cohort_id, count(*) FROM objections
            GROUP BY 1, 2, 3 ORDER BY 4 DESC
            """
        )
    ).fetchall()
    out += _section(
        "Objections (FR-4.3 — this is the market research)",
        [f"- {t} / {co}: {cl} x{n}" for t, cl, co, n in objections],
    )

    winloss = conn.execute(
        text(
            """
            SELECT DISTINCT ON (tenant) tenant, summary, findings FROM research_notes
            WHERE level = 'winloss' ORDER BY tenant, created_at DESC
            """
        )
    ).fetchall()
    out += _section(
        "Why we win / why we lose (FR-5.5)",
        [
            f"- {t}: {s}\n  win: {'; '.join(f.get('why_we_win', []))}\n"
            f"  lose: {'; '.join(f.get('why_we_lose', []))}"
            for t, s, f in winloss
        ],
    )

    variants = conn.execute(
        text(
            """
            SELECT tenant, variant_id, trials, successes, bounces, complaints, paused
            FROM variant_stats ORDER BY tenant, successes DESC, trials DESC
            """
        )
    ).fetchall()
    out += _section(
        "Variants",
        [
            f"- {t}/{v}: {s}/{tr} positive"
            + (f", bounces={b}" if b else "")
            + (f", complaints={c}" if c else "")
            + (" [PAUSED]" if paused else "")
            for t, v, tr, s, b, c, paused in variants
        ],
    )

    escalations = conn.execute(
        text(
            """
            SELECT id, subject_type, reason, created_at FROM escalations
            WHERE status = 'open' ORDER BY created_at ASC
            """
        )
    ).fetchall()
    out += _section(
        "Open escalations (reachout approve)",
        [f"- {eid} [{st}] {r} ({c:%Y-%m-%d %H:%M})" for eid, st, r, c in escalations],
    )

    props = conn.execute(
        text(
            """
            SELECT kind, summary FROM proposals
            WHERE status = 'open' ORDER BY created_at ASC
            """
        )
    ).fetchall()
    out += _section(
        "Open proposals (reachout approve)", [f"- [{k}] {s}" for k, s in props]
    )

    suppressed = conn.execute(text("SELECT count(*) FROM suppressions")).scalar()
    tombstones = conn.execute(text("SELECT count(*) FROM forget_tombstones")).scalar()
    halted = conn.execute(text("SELECT scope, flag FROM control_flags")).fetchall()
    out += _section(
        "Compliance",
        [f"- suppressions: {suppressed}, forget tombstones: {tombstones}"]
        + [f"- ACTIVE FLAG: {s} ({f})" for s, f in halted],
    )

    queues = conn.execute(
        text("SELECT queue, status, count(*) FROM jobs GROUP BY queue, status ORDER BY queue")
    ).fetchall()
    dead = [f"- DLQ {q}: {n}" for q, s, n in queues if s == "dead"]
    backlog = [f"- {q} {s}: {n}" for q, s, n in queues if s in ("ready", "leased")]
    out += _section("Queues", backlog + dead)

    return "\n".join(out)
