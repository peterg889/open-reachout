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
:root{
  --paper:#fbfaf7;--panel:#ffffff;--line:#ddd6c8;--hairline:#ece7db;
  --ink:#211c15;--muted:#6b6356;--faint:#968c7c;
  --green-dark:#15302b;--accent:#b97a26;
  --good:#1e7a43;--good-bg:#e7f3ec;--live:#1c6e75;--live-bg:#e5f1f2;
  --warm:#9a6610;--warm-bg:#faf0dc;--exit:#a83c2a;--exit-bg:#f9e9e5;
  --idle:#6b6356;--idle-bg:#efece5;
  --serif:"Iowan Old Style","Palatino Nova",Palatino,"Book Antiqua",Georgia,serif;
  --mono:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
body{font-family:var(--serif);margin:0;background:var(--paper);color:var(--ink);
  font-size:17px;line-height:1.55}
header{background:var(--green-dark);color:#f3efe6;padding:16px 32px;
  display:flex;gap:18px;align-items:baseline}
header a{color:#f3efe6;text-decoration:none;font-weight:600;font-size:1.05rem;
  letter-spacing:.02em}
header .small{margin-left:auto;color:#c8c0ad;font-size:.8rem}
main{max-width:1100px;margin:28px auto 80px;padding:0 24px}
h1{font-size:1.8rem;font-weight:600;margin:0 0 6px}
h1::after{content:"";display:block;width:56px;height:3px;background:var(--accent);
  margin-top:10px}
h2{font-size:1.12rem;font-weight:600;margin:36px 0 12px;color:var(--green-dark);
  border-bottom:2px solid var(--green-dark);padding-bottom:5px}
a{color:#1c6e75}
code{font-family:var(--mono);font-size:.85em;background:var(--idle-bg);
  padding:1px 5px;border-radius:4px}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  border-top:3px solid var(--green-dark);padding:14px 20px;min-width:130px}
.card .n{font-family:var(--mono);font-size:1.8rem;font-weight:600;
  font-variant-numeric:tabular-nums}
.card .l{font-size:.8rem;color:var(--muted);margin-top:3px}
table{border-collapse:collapse;width:100%;background:var(--panel);
  border:1px solid var(--line);border-radius:8px;overflow:hidden}
th{font-size:.8rem;font-weight:700;color:var(--ink);background:#f1ecdf;
  padding:10px 12px;text-align:left;border-bottom:1px solid var(--line)}
td{padding:10px 12px;border-bottom:1px solid var(--hairline);font-size:.95rem;
  vertical-align:top}
tr:last-child td{border-bottom:none}
table tr:hover td{background:#faf5e9}
.num,td.num{font-family:var(--mono);font-variant-numeric:tabular-nums;
  font-size:.9rem}
.tag{display:inline-block;font-family:var(--mono);font-size:.74rem;
  background:var(--idle-bg);border-radius:6px;padding:2px 8px;margin:1px 4px 1px 0;
  color:var(--ink)}
.state{display:inline-block;font-family:var(--mono);font-size:.74rem;
  font-weight:600;padding:2px 10px;border-radius:999px}
.state.good{color:var(--good);background:var(--good-bg)}
.state.live{color:var(--live);background:var(--live-bg)}
.state.warm{color:var(--warm);background:var(--warm-bg)}
.state.exit{color:var(--exit);background:var(--exit-bg)}
.state.idle{color:var(--idle);background:var(--idle-bg)}
.paused{color:var(--exit);font-weight:700}
.note{background:#fffbe9;border:1px solid #e7d9a0;border-left:4px solid var(--accent);
  border-radius:8px;padding:14px 18px;margin:14px 0;font-size:.97rem}
.note b{color:var(--green-dark)}
.msg{border:1px solid var(--line);border-left:4px solid var(--green-dark);
  border-radius:8px;padding:12px 16px;margin:12px 0;background:var(--panel);
  max-width:90%}
.msg.in{border-left-color:var(--good);background:var(--good-bg);margin-left:10%}
.msg .meta{font-size:.78rem;color:var(--muted);margin-bottom:7px}
.msg pre{line-height:1.55;font-size:.95rem}
.bar{background:linear-gradient(90deg,#27554c,var(--green-dark));height:14px;
  border-radius:4px;min-width:3px}
pre{white-space:pre-wrap;font-family:inherit;margin:0}
.small{font-size:.8rem;color:var(--muted)}
.crumb{font-size:.85rem;color:var(--muted);margin:0 0 14px}
.crumb a{color:var(--muted)}
.crumb a:hover{color:var(--green-dark)}
button{font-family:var(--mono);font-size:.78rem;font-weight:600;color:#fff;
  background:var(--green-dark);border:1px solid var(--green-dark);
  border-radius:6px;padding:6px 14px;margin:2px 6px 2px 0;cursor:pointer}
button:hover{background:#0e211e}
button.danger{background:var(--panel);color:var(--exit);border-color:var(--exit)}
button.danger:hover{background:var(--exit-bg)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
"""


def _e(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _page(title: str, body: str, crumb: str = "") -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_e(title)} — Open Reachout</title><style>{_STYLE}</style></head>"
        f"<body><header><a href='/dashboard'>Open&thinsp;Reachout</a>"
        f"<span class='small'>read-only operator dashboard — nothing sends from here</span>"
        f"</header><main>{crumb}<h1>{_e(title)}</h1>{body}</main></body></html>"
    )


#: Prospect/strategy states rendered as the color-coded state machine.
_STATE_CLASS = {
    "converted": "good", "engaged": "live", "contacted": "warm", "queued": "warm",
    "qualified": "live", "enriched": "idle", "discovered": "idle",
    "declined": "exit", "unsubscribed": "exit", "bounced": "exit",
    "disqualified": "exit", "unenrichable": "exit", "no_response": "idle",
    "forgotten": "exit",
}


def _state(value: object) -> str:
    cls = _STATE_CLASS.get(str(value), "idle")
    return f"<span class='state {cls}'>{_e(value)}</span>"


_ACRONYMS = {"nj", "tx", "ga", "us", "lcsw", "lpc", "lmft", "lac", "npi", "faq"}
_QUARTER = __import__("re").compile(r"^(20\d{2})q([1-4])$")


def _friendly(slug: object) -> str:
    """Human display name for a snake_case id: 'nj_lcsw_lpc_2026q3' ->
    'NJ LCSW LPC — 2026 Q3'. The raw id stays visible as secondary text."""
    words: list[str] = []
    suffix = ""
    for part in str(slug).split("_"):
        m = _QUARTER.match(part)
        if m:
            suffix = f" — {m.group(1)} Q{m.group(2)}"
        elif part.lower() in _ACRONYMS:
            words.append(part.upper())
        else:
            words.append(part.capitalize())
    return (" ".join(words) or str(slug)) + suffix


def _named(slug: object) -> str:
    return f"{_e(_friendly(slug))} <span class='small'>({_e(slug)})</span>"


def _ledger_rows(conn, prospect_id: str) -> str:  # noqa: ANN001
    """The prospect's state ledger: every transition with its reason, plus
    escalations — the answer to 'why is this prospect in this state?'."""
    rows = conn.execute(
        text(
            """
            SELECT created_at, event, payload, actor FROM audit_events
            WHERE subject_type = 'prospect' AND subject_id = :p
            ORDER BY created_at
            """
        ),
        {"p": prospect_id},
    ).fetchall()
    esc = conn.execute(
        text(
            """
            SELECT created_at, reason, status FROM escalations
            WHERE subject_type = 'prospect' AND subject_id = :p
            ORDER BY created_at
            """
        ),
        {"p": prospect_id},
    ).fetchall()
    out = []
    for when, event, payload, actor in rows:
        payload = payload or {}
        if event == "transition":
            change = f"{_state(payload.get('from'))} → {_state(payload.get('to'))}"
            why = payload.get("reason") or ""
        else:
            change = f"<span class='tag'>{_e(event)}</span>"
            why = payload.get("reason") or payload.get("note") or ""
        out.append(
            f"<tr><td class='small num'>{when:%Y-%m-%d %H:%M}</td>"
            f"<td>{change}</td><td>{_e(why) or '<span class=small>—</span>'}</td>"
            f"<td class='small'>{_e(actor)}</td></tr>"
        )
    for when, reason, status in esc:
        out.append(
            f"<tr><td class='small num'>{when:%Y-%m-%d %H:%M}</td>"
            f"<td><span class='state {'warm' if status == 'open' else 'idle'}'>"
            f"escalated{'' if status == 'open' else ' (resolved)'}</span></td>"
            f"<td>{_e(reason)}</td><td class='small'>review queue</td></tr>"
        )
    return "".join(out) or "<tr><td colspan='4' class='small'>(no events yet)</td></tr>"


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
                    f"<tr><td>{_e(stage)}</td><td class='num'>{n}</td>"
                    f"<td style='width:45%'><div class='bar' "
                    f"style='width:{max(int(100 * n / top), 1)}%'></div></td></tr>"
                    for stage, n in f.reached
                )
                exit_rows = "".join(
                    f"<tr><td>{_e(label)}</td><td class='num'>{n}</td></tr>"
                    for label, n in sorted(f.exits.items(), key=lambda kv: -kv[1])
                ) or "<tr><td colspan='2' class='small'>(none — nobody lost yet)</td></tr>"
                cohort_table = "".join(
                    f"<tr><td><a href='/dashboard/cohort/{_e(c.cohort_id)}"
                    f"?tenant={_e(tenant)}'>{_e(_friendly(c.cohort_id))}</a>"
                    f"<br><span class='small'>{_e(c.cohort_id)}</span></td>"
                    f"<td class='small'>{_e(c.persona_id)}</td>"
                    f"<td class='num'>{c.members}</td>"
                    f"<td class='num'>{c.contacted}</td><td class='num'>{c.replies}</td>"
                    f"<td class='num'>{c.converted}</td></tr>"
                    for c in cohort_rows
                ) or "<tr><td colspan='6' class='small'>(no prospects yet)</td></tr>"
                escal = len(escalations.list_open(conn, tenant))
                props = len(proposals.list_open(conn, tenant))
                sections.append(
                    f"<h2><a href='/dashboard/campaign/{_e(tenant)}'>"
                    f"{_e(_friendly(tenant))}</a> "
                    f"<span class='small'>campaign — click for research &amp; "
                    f"cohorts</span></h2>{cards}"
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

    @router.get("/dashboard/campaign/{tenant}", response_class=HTMLResponse)
    def campaign(tenant: str, request: Request) -> HTMLResponse:
        _check_token(request)
        with engine.begin() as conn:
            f = metrics.funnel(conn, tenant)
            cohort_rows = metrics.cohorts(conn, tenant)
            market = research.latest(conn, tenant, "campaign", tenant)
            winloss = research.latest(conn, tenant, "winloss", tenant)
            escal = len(escalations.list_open(conn, tenant))
            props = len(proposals.list_open(conn, tenant))
        cards = _cards([
            ("reached (contacted)", f.contacted), ("replies", f.replies),
            ("converted", f.converted),
            ("conversion", f"{f.conversion_rate:.0%}" if f.contacted else "—"),
            ("open escalations", escal), ("open proposals", props),
        ])
        research_html = (
            f"<div class='note'><b>Campaign research — market view</b> "
            f"<span class='small'>({market.created_at:%Y-%m-%d %H:%M})</span>"
            f"<br>{_e(market.summary)}</div>"
            if market else
            "<div class='note small'>No campaign-tier research yet — run "
            "<code>reachout research</code>.</div>"
        )
        if winloss:
            research_html += (
                f"<div class='note'><b>Why we win / why we lose</b>"
                f"<br>{_e(winloss.summary)}</div>"
            )
        cohort_html = "".join(
            f"<tr><td><a href='/dashboard/cohort/{_e(c.cohort_id)}"
            f"?tenant={_e(tenant)}'>{_e(_friendly(c.cohort_id))}</a>"
            f"<br><span class='small'>{_e(c.cohort_id)}</span></td>"
            f"<td class='small'>{_e(_friendly(c.persona_id))}</td>"
            f"<td class='num'>{c.members}</td><td class='num'>{c.contacted}</td>"
            f"<td class='num'>{c.replies}</td><td class='num'>{c.converted}</td></tr>"
            for c in cohort_rows
        ) or "<tr><td colspan='6' class='small'>(no prospects yet)</td></tr>"
        crumb = f"<p class='crumb'><a href='/dashboard'>overview</a> / {_e(tenant)}</p>"
        return _page(
            f"Campaign: {_friendly(tenant)}",
            cards + research_html
            + "<h2>Cohorts in this campaign</h2>"
            + "<table><tr><th>cohort</th><th>persona</th><th>members</th>"
              "<th>contacted</th><th>replies</th><th>converted</th></tr>"
            + cohort_html + "</table>",
            crumb=crumb,
        )

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
            "<tr><td class='num'>{v}</td><td>{tags}</td><td class='num'>{tr}</td>"
            "<td class='num'>{s}</td><td class='num'>{rate}</td>"
            "<td>{state}</td><td class='small'>{note}</td></tr>".format(
                v=_e(s.variant_id),
                tags="".join(f"<span class='tag'>{_e(k)}={_e(v)}</span>"
                             for k, v in s.attributes.items()),
                tr=s.trials, s=s.successes,
                rate=f"{s.success_rate:.0%}" if s.trials else "—",
                state=(
                    "<span class='state exit'>paused</span>" if s.paused
                    else "<span class='state live'>live</span>"
                ),
                note=_e(strategy_notes[s.variant_id].summary)
                if strategy_notes.get(s.variant_id) else "",
            )
            for s in strategy_rows
        ) or "<tr><td colspan='7'>(no sends yet)</td></tr>"
        member_html = "".join(
            f"<tr><td><a href='/dashboard/member/{m.prospect_id}"
            f"?tenant={_e(tenant)}'>{_e(m.display_name)}</a></td>"
            f"<td>{_state(m.state)}</td>"
            f"<td class='small num'>{_e(m.email_confidence or '—')}</td>"
            f"<td class='num'>{m.touches}</td><td class='num'>{m.replies}</td></tr>"
            for m in member_rows
        ) or "<tr><td colspan='5' class='small'>(none)</td></tr>"
        crumb = (
            f"<p class='crumb'><a href='/dashboard'>overview</a> / "
            f"<a href='/dashboard/campaign/{_e(tenant)}'>{_e(tenant)}</a>"
            f" / {_e(cohort_id)}</p>"
        )
        return _page(
            f"Cohort: {_friendly(cohort_id)}",
            cards + note_html, crumb=crumb
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
            ledger = _ledger_rows(conn, prospect_id) if detail else ""
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
        crumb = (
            f"<p class='crumb'><a href='/dashboard'>overview</a> / "
            f"<a href='/dashboard/campaign/{_e(tenant)}'>{_e(tenant)}</a> / "
            f"<a href='/dashboard/cohort/{_e(detail.cohort_id)}?tenant={_e(tenant)}'>"
            f"{_e(_friendly(detail.cohort_id))}</a> / {_e(detail.display_name)}</p>"
        )
        return _page(
            detail.display_name,
            _cards([("state", detail.state), ("cohort", detail.cohort_id),
                      ("persona", detail.persona_id),
                      ("source", f"{detail.source_adapter} ({detail.data_basis})")])
            + "<h2>Background research (Evidence Card)</h2>"
            + "<table><tr><th>type</th><th>fact</th><th>provenance</th></tr>"
            + evidence_html + "</table>"
            + "<h2>Conversation history</h2>" + convo_html
            + "<h2>State ledger — why this prospect is where it is</h2>"
            + "<table><tr><th>when</th><th>event</th><th>reason</th>"
              "<th>actor</th></tr>" + ledger + "</table>",
            crumb=crumb,
        )

    return router
