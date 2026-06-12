"""Read-only operator dashboard (PRD RX-1): cohorts -> strategies -> members
-> research -> conversations, plus top-level funnel metrics with abandonment.

Server-rendered HTML, zero JS dependencies. Optional token gate via
OR_DASHBOARD_TOKEN (?token=...); bind to localhost otherwise.
"""

from __future__ import annotations

import html
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.engine import Engine

from open_reachout.core import escalations, metrics, proposals, research

_STYLE = """
body{font-family:system-ui,sans-serif;margin:0;background:#f6f5f1;color:#1d1d1f}
header{background:#15302b;color:#f3efe6;padding:14px 28px;display:flex;gap:18px;
  align-items:baseline}
header a{color:#f3efe6;text-decoration:none;font-weight:600}
main{max-width:1100px;margin:24px auto;padding:0 20px}
h1{font-size:1.4rem}h2{font-size:1.05rem;margin-top:28px;border-bottom:2px solid #15302b;
  padding-bottom:4px}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin:14px 0}
.card{background:#fff;border:1px solid #ddd6c8;border-radius:10px;padding:12px 18px;
  min-width:120px}
.card .n{font-size:1.7rem;font-weight:700}.card .l{font-size:.78rem;color:#666}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #ddd6c8}
th,td{padding:7px 10px;border-bottom:1px solid #eee8da;text-align:left;font-size:.88rem}
th{background:#efe9db}
.tag{display:inline-block;background:#e7e1d2;border-radius:6px;padding:1px 8px;
  font-size:.75rem;margin-right:4px}
.paused{color:#a33;font-weight:700}
.note{background:#fffbe9;border:1px solid #e7d9a0;border-radius:8px;padding:10px 14px;
  font-size:.88rem;margin:10px 0}
.msg{border:1px solid #ddd6c8;border-radius:10px;padding:10px 14px;margin:10px 0;
  background:#fff}
.msg.in{background:#eef4ee;border-color:#bcd3bc;margin-left:48px}
.msg .meta{font-size:.75rem;color:#666;margin-bottom:6px}
.bar{background:#15302b;height:14px;border-radius:4px}
pre{white-space:pre-wrap;font-family:inherit;margin:0}
.small{font-size:.78rem;color:#666}
"""


def _e(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_e(title)} — Open Reachout</title><style>{_STYLE}</style></head>"
        f"<body><header><a href='/dashboard'>Open Reachout</a>"
        f"<span class='small'>read-only operator dashboard</span></header>"
        f"<main><h1>{_e(title)}</h1>{body}</main></body></html>"
    )


def _check_token(request: Request) -> None:
    expected = os.environ.get("OR_DASHBOARD_TOKEN", "")
    if expected and request.query_params.get("token", "") != expected:
        raise HTTPException(401, "dashboard token required (?token=...)")


def _cards(items: list[tuple[str, object]]) -> str:
    return (
        "<div class='cards'>"
        + "".join(f"<div class='card'><div class='n'>{_e(n)}</div>"
                  f"<div class='l'>{_e(label)}</div></div>" for label, n in items)
        + "</div>"
    )


def build_dashboard_router(engine: Engine) -> APIRouter:
    router = APIRouter()

    @router.get("/dashboard", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        _check_token(request)
        with engine.begin() as conn:
            tenants = [r[0] for r in conn.execute(
                text("SELECT slug FROM tenants ORDER BY slug")).fetchall()]
            sections = []
            for tenant in tenants:
                f = metrics.funnel(conn, tenant)
                cohort_rows = metrics.cohorts(conn, tenant)
                cards = _cards([
                    ("reached (contacted)", f.contacted),
                    ("replies", f.replies),
                    ("positive replies", f.positive_replies),
                    ("in conversation", f.in_conversation),
                    ("converted", f.converted),
                    ("conversion", f"{f.conversion_rate:.0%}" if f.contacted else "—"),
                ])
                top = max((n for _, n in f.reached), default=0) or 1
                funnel_rows = "".join(
                    f"<tr><td>{_e(stage)}</td><td>{n}</td>"
                    f"<td style='width:45%'><div class='bar' "
                    f"style='width:{int(100 * n / top)}%'></div></td></tr>"
                    for stage, n in f.reached
                )
                exit_rows = "".join(
                    f"<tr><td>{_e(label)}</td><td>{n}</td></tr>"
                    for label, n in sorted(f.exits.items(), key=lambda kv: -kv[1])
                ) or "<tr><td colspan='2'>(none)</td></tr>"
                cohort_table = "".join(
                    f"<tr><td><a href='/dashboard/cohort/{_e(c.cohort_id)}"
                    f"?tenant={_e(tenant)}'>{_e(c.cohort_id)}</a></td>"
                    f"<td>{_e(c.persona_id)}</td><td>{c.members}</td>"
                    f"<td>{c.contacted}</td><td>{c.replies}</td><td>{c.converted}</td></tr>"
                    for c in cohort_rows
                ) or "<tr><td colspan='6'>(no prospects yet)</td></tr>"
                escal = len(escalations.list_open(conn, tenant))
                props = len(proposals.list_open(conn, tenant))
                sections.append(
                    f"<h2>{_e(tenant)}</h2>{cards}"
                    f"<h2>Cohorts the system is working</h2>"
                    f"<table><tr><th>cohort</th><th>persona</th><th>members</th>"
                    f"<th>contacted</th><th>replies</th><th>converted</th></tr>"
                    f"{cohort_table}</table>"
                    f"<h2>Funnel — where people are in the flow</h2>"
                    f"<table><tr><th>reached stage</th><th>#</th><th></th></tr>"
                    f"{funnel_rows}</table>"
                    f"<h2>Abandonment — where the flow loses people</h2>"
                    f"<table><tr><th>where</th><th>#</th></tr>{exit_rows}</table>"
                    f"<p class='small'>{escal} open escalation(s), {props} open "
                    f"proposal(s) — work them with <code>reachout approve</code>.</p>"
                )
        return _page("Overview", "".join(sections) or "<p>No tenants yet.</p>")

    @router.get("/dashboard/cohort/{cohort_id}", response_class=HTMLResponse)
    def cohort(cohort_id: str, tenant: str, request: Request) -> HTMLResponse:
        _check_token(request)
        with engine.begin() as conn:
            f = metrics.funnel(conn, tenant, cohort_id)
            note = research.latest(conn, tenant, "cohort", cohort_id)
            member_rows = metrics.members(conn, tenant, cohort_id)
            strategy_rows = metrics.strategies(conn, tenant)
            strategy_notes = {
                s.variant_id: research.latest(conn, tenant, "strategy", s.variant_id)
                for s in strategy_rows
            }
        cards = _cards([
            ("members", len(member_rows)), ("contacted", f.contacted),
            ("replies", f.replies), ("converted", f.converted),
        ])
        note_html = (
            f"<div class='note'><b>Cohort research</b> "
            f"<span class='small'>({note.created_at:%Y-%m-%d %H:%M})</span>"
            f"<br>{_e(note.summary)}</div>"
            if note else
            "<div class='note small'>No cohort research yet — run "
            "<code>reachout research</code>.</div>"
        )
        strat_html = "".join(
            "<tr><td>{v}</td><td>{tags}</td><td>{tr}</td><td>{s}</td><td>{rate}</td>"
            "<td>{state}</td><td class='small'>{note}</td></tr>".format(
                v=_e(s.variant_id),
                tags="".join(f"<span class='tag'>{_e(k)}={_e(v)}</span>"
                             for k, v in s.attributes.items()),
                tr=s.trials, s=s.successes,
                rate=f"{s.success_rate:.0%}" if s.trials else "—",
                state="<span class='paused'>PAUSED</span>" if s.paused else "live",
                note=_e(strategy_notes[s.variant_id].summary)
                if strategy_notes.get(s.variant_id) else "",
            )
            for s in strategy_rows
        ) or "<tr><td colspan='7'>(no sends yet)</td></tr>"
        member_html = "".join(
            f"<tr><td><a href='/dashboard/member/{m.prospect_id}"
            f"?tenant={_e(tenant)}'>{_e(m.display_name)}</a></td>"
            f"<td>{_e(m.state)}</td><td>{_e(m.email_confidence or '—')}</td>"
            f"<td>{m.touches}</td><td>{m.replies}</td></tr>"
            for m in member_rows
        ) or "<tr><td colspan='5'>(none)</td></tr>"
        return _page(
            f"Cohort: {cohort_id}",
            cards + note_html
            + "<h2>Strategies being tested (bandit arms)</h2>"
            + "<table><tr><th>variant</th><th>attributes</th><th>sends</th>"
              "<th>positive</th><th>rate</th><th>status</th><th>research</th></tr>"
            + strat_html + "</table>"
            + "<h2>Members</h2>"
            + "<table><tr><th>member</th><th>state</th><th>email</th>"
              "<th>sent</th><th>replies</th></tr>"
            + member_html + "</table>",
        )

    @router.get("/dashboard/member/{prospect_id}", response_class=HTMLResponse)
    def member(prospect_id: str, tenant: str, request: Request) -> HTMLResponse:
        _check_token(request)
        with engine.begin() as conn:
            detail = metrics.member_detail(conn, prospect_id)
        if detail is None:
            raise HTTPException(404, "no such prospect")
        evidence_html = "".join(
            f"<tr><td><span class='tag'>{_e(e[0])}</span></td>"
            f"<td><pre>{_e(e[1])}</pre></td>"
            f"<td class='small'><a href='{_e(e[2])}'>{_e(e[2])}</a><br>"
            f"observed {_e(e[3])}</td></tr>"
            for e in detail.evidence
        ) or "<tr><td colspan='3'>(no research yet)</td></tr>"
        convo_html = "".join(
            f"<div class='msg {'in' if c.direction == 'in' else ''}'>"
            f"<div class='meta'>{'⟵ reply' if c.direction == 'in' else '⟶ sent'}"
            f" · {_e(c.when or 'draft')} · {_e(c.status or '')}"
            + (f" · variant {_e(c.variant_id)}" if c.variant_id else "")
            + "</div>"
            + (f"<b>{_e(c.subject)}</b><br>" if c.subject else "")
            + f"<pre>{_e(c.body or '(scrubbed)')}</pre></div>"
            for c in detail.conversation
        ) or "<p class='small'>(no outreach yet)</p>"
        back = f"<p><a href='/dashboard/cohort/{_e(detail.cohort_id)}?tenant={_e(tenant)}'>" \
               f"&larr; back to cohort {_e(detail.cohort_id)}</a></p>"
        return _page(
            detail.display_name,
            back
            + _cards([("state", detail.state), ("cohort", detail.cohort_id),
                      ("persona", detail.persona_id),
                      ("source", f"{detail.source_adapter} ({detail.data_basis})")])
            + "<h2>Background research (Evidence Card)</h2>"
            + "<table><tr><th>type</th><th>fact</th><th>provenance</th></tr>"
            + evidence_html + "</table>"
            + "<h2>Conversation history</h2>" + convo_html,
        )

    return router
