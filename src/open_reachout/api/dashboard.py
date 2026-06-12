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
  --ink:#171310;--ink2:#1e1914;--panel:#241e17;--panel2:#2a231b;
  --line:#3a3127;--hairline:#332b22;
  --paper:#ece3d3;--muted:#a3927a;--faint:#7a6c58;
  --amber:#e3a23c;--amber-dim:#8a6526;
  --green:#85b894;--red:#d97f64;--teal:#82b5ac;
  --serif:"Iowan Old Style","Palatino Nova",Palatino,"Book Antiqua",Georgia,serif;
  --mono:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
body{font-family:var(--serif);margin:0;color:var(--paper);background:
  radial-gradient(1200px 500px at 15% -10%,#2b2014 0%,transparent 60%),
  radial-gradient(900px 420px at 110% 0%,#231c12 0%,transparent 55%),
  var(--ink);min-height:100vh}
body::after{content:"";position:fixed;inset:0;pointer-events:none;opacity:.05;
  background:repeating-linear-gradient(0deg,transparent 0 2px,#000 2px 3px)}
header{padding:20px 32px 14px;display:flex;align-items:baseline;gap:18px;
  border-bottom:1px solid var(--line);position:relative}
header::after{content:"";position:absolute;left:0;right:0;bottom:3px;
  border-bottom:1px solid var(--hairline)}
header a{color:var(--paper);text-decoration:none;font-family:var(--mono);
  font-size:.82rem;letter-spacing:.42em;text-transform:uppercase}
header a:hover{color:var(--amber)}
header .small{margin-left:auto;letter-spacing:.18em;text-transform:uppercase;
  font-family:var(--mono);font-size:.62rem;color:var(--faint)}
main{max-width:1100px;margin:30px auto 80px;padding:0 24px;
  animation:rise .45s ease-out both}
h1{font-size:1.9rem;font-weight:500;letter-spacing:.01em;margin:0 0 4px;
  color:var(--paper)}
h1::after{content:"";display:block;width:64px;height:3px;background:var(--amber);
  margin-top:10px}
h2{font-family:var(--mono);font-size:.72rem;font-weight:600;color:var(--amber);
  letter-spacing:.3em;text-transform:uppercase;margin:40px 0 12px;
  display:flex;align-items:center;gap:12px}
h2::after{content:"";flex:1;height:1px;
  background:linear-gradient(90deg,var(--line),transparent)}
a{color:var(--teal)}a:hover{color:var(--paper)}
code{font-family:var(--mono);font-size:.85em;color:var(--amber)}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0 6px}
.card{background:linear-gradient(180deg,var(--panel2),var(--panel));
  border:1px solid var(--line);border-top:2px solid var(--amber-dim);
  padding:14px 18px 12px;min-width:128px;flex:0 1 auto;
  animation:rise .5s ease-out both;transition:border-color .2s,transform .2s}
.card:hover{border-top-color:var(--amber);transform:translateY(-2px)}
.cards .card:nth-child(1){animation-delay:.03s}.cards .card:nth-child(2){animation-delay:.07s}
.cards .card:nth-child(3){animation-delay:.11s}.cards .card:nth-child(4){animation-delay:.15s}
.cards .card:nth-child(5){animation-delay:.19s}.cards .card:nth-child(6){animation-delay:.23s}
.card .n{font-family:var(--mono);font-size:1.75rem;font-weight:600;
  color:var(--paper);font-variant-numeric:tabular-nums}
.card .l{font-family:var(--mono);font-size:.6rem;letter-spacing:.18em;
  text-transform:uppercase;color:var(--muted);margin-top:4px}
table{border-collapse:collapse;width:100%;background:var(--ink2);
  border:1px solid var(--line)}
th{font-family:var(--mono);font-size:.62rem;font-weight:600;letter-spacing:.16em;
  text-transform:uppercase;color:var(--muted);background:var(--panel);
  padding:9px 12px;text-align:left;border-bottom:1px solid var(--line)}
td{padding:9px 12px;border-bottom:1px solid var(--hairline);font-size:.9rem;
  vertical-align:top}
tr:last-child td{border-bottom:none}
tbody tr,table tr{transition:background .15s}
table tr:hover td{background:rgba(227,162,60,.045)}
td a{text-decoration:none;border-bottom:1px dotted var(--teal)}
.num,td.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.tag{display:inline-block;font-family:var(--mono);font-size:.66rem;
  letter-spacing:.06em;border:1px solid var(--line);color:var(--muted);
  padding:1px 8px;margin:1px 4px 1px 0;background:var(--panel)}
.state{display:inline-block;font-family:var(--mono);font-size:.64rem;
  letter-spacing:.14em;text-transform:uppercase;padding:2px 9px;
  border:1px solid currentColor;background:transparent}
.state.good{color:var(--green)}.state.warm{color:var(--amber)}
.state.live{color:var(--teal)}.state.exit{color:var(--red)}
.state.idle{color:var(--faint)}
.paused{color:var(--red);font-family:var(--mono);font-size:.7rem;
  letter-spacing:.14em}
.note{background:linear-gradient(180deg,var(--panel2),var(--panel));
  border:1px solid var(--line);border-left:3px solid var(--amber);
  padding:14px 18px;margin:14px 0;font-size:.92rem;line-height:1.55}
.note b{font-family:var(--mono);font-size:.62rem;letter-spacing:.22em;
  text-transform:uppercase;color:var(--amber);display:block;margin-bottom:6px}
.msg{border:1px solid var(--line);border-left:3px solid var(--amber-dim);
  background:var(--ink2);padding:12px 16px;margin:12px 0;max-width:88%}
.msg.in{border-left-color:var(--green);background:var(--panel);
  margin-left:12%}
.msg .meta{font-family:var(--mono);font-size:.62rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--faint);margin-bottom:8px}
.msg b{font-family:var(--serif);font-size:1.02rem;font-weight:600}
.msg pre{line-height:1.55;font-size:.92rem}
.bar{background:linear-gradient(90deg,var(--amber-dim),var(--amber));
  height:13px;min-width:2px;position:relative;transition:filter .15s}
tr:hover .bar{filter:brightness(1.18)}
pre{white-space:pre-wrap;font-family:inherit;margin:0}
.small{font-size:.74rem;color:var(--muted)}
.crumb{font-family:var(--mono);font-size:.68rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--faint);margin:0 0 14px}
.crumb a{color:var(--muted);text-decoration:none}
.crumb a:hover{color:var(--amber)}
button{font-family:var(--mono);font-size:.66rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--paper);background:var(--panel2);
  border:1px solid var(--amber-dim);padding:5px 14px;margin:2px 6px 2px 0;
  cursor:pointer;transition:all .15s}
button:hover{background:var(--amber);color:var(--ink);border-color:var(--amber)}
button.danger{border-color:#7c4034;color:var(--red)}
button.danger:hover{background:var(--red);color:var(--ink)}
:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
@keyframes rise{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""


def _e(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _page(title: str, body: str, crumb: str = "") -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_e(title)} — Open Reachout</title><style>{_STYLE}</style></head>"
        f"<body><header><a href='/dashboard'>Open&thinsp;Reachout</a>"
        f"<span class='small'>operator ledger · nothing sends from this page</span>"
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
                    f"?tenant={_e(tenant)}'>{_e(c.cohort_id)}</a></td>"
                    f"<td class='small'>{_e(c.persona_id)}</td>"
                    f"<td class='num'>{c.members}</td>"
                    f"<td class='num'>{c.contacted}</td><td class='num'>{c.replies}</td>"
                    f"<td class='num'>{c.converted}</td></tr>"
                    for c in cohort_rows
                ) or "<tr><td colspan='6' class='small'>(no prospects yet)</td></tr>"
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
            f"<p class='crumb'><a href='/dashboard'>overview</a>"
            f" / {_e(tenant)} / {_e(cohort_id)}</p>"
        )
        return _page(
            f"Cohort: {cohort_id}",
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
            f"<p class='crumb'><a href='/dashboard'>overview</a> / {_e(tenant)} / "
            f"<a href='/dashboard/cohort/{_e(detail.cohort_id)}?tenant={_e(tenant)}'>"
            f"{_e(detail.cohort_id)}</a> / {_e(detail.display_name)}</p>"
        )
        return _page(
            detail.display_name,
            _cards([("state", detail.state), ("cohort", detail.cohort_id),
                      ("persona", detail.persona_id),
                      ("source", f"{detail.source_adapter} ({detail.data_basis})")])
            + "<h2>Background research (Evidence Card)</h2>"
            + "<table><tr><th>type</th><th>fact</th><th>provenance</th></tr>"
            + evidence_html + "</table>"
            + "<h2>Conversation history</h2>" + convo_html,
            crumb=crumb,
        )

    return router
