"""The dashboard's management surface (PRD FR-9.1/9.4, spec 11.4).

Every mutation goes through the same service-layer functions as the CLI —
the UI holds no privileged path: gates (suppression, budgets, halt, the
always-human set) sit below this layer, so a dashboard bug can degrade UX
but cannot widen authority.

Auth: management requires `OR_DASHBOARD_MANAGE_TOKEN` (the `manage:write`
scope of spec 11.1). Unset = the management surface is OFF and the dashboard
stays read-only. Actions are plain POST forms (htmx-friendly, no JS needed).
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.engine import Engine

from open_reachout.api.dashboard import _e, _page
from open_reachout.core import control, escalations, human_tasks, proposals, sendpath
from open_reachout.core.config import Brief
from open_reachout.core.programs import enqueue_synthesis

ACTOR = "operator:dashboard"


def _check_manage(request: Request) -> str:
    expected = os.environ.get("OR_DASHBOARD_MANAGE_TOKEN", "")
    if not expected:
        raise HTTPException(403, "management surface disabled (no manage token configured)")
    token = request.query_params.get("manage_token", "")
    if token != expected:
        raise HTTPException(401, "manage token required (?manage_token=...)")
    return token


def _form(action: str, label: str, token: str, danger: bool = False) -> str:
    cls = " class='danger'" if danger else ""
    return (
        f"<form method='post' action='{_e(action)}?manage_token={_e(token)}' "
        f"style='display:inline'><button{cls}>{_e(label)}</button></form>"
    )


def build_manage_router(engine: Engine) -> APIRouter:
    router = APIRouter()

    @router.get("/dashboard/queues", response_class=HTMLResponse)
    def queues(request: Request) -> HTMLResponse:
        token = _check_manage(request)
        with engine.begin() as conn:
            props = proposals.list_open(conn)
            escs = escalations.list_open(conn)
            held = sendpath.list_pending_review(conn)
            tasks = human_tasks.list_pending(conn)
        rows: list[str] = [
            "<p class='crumb'><a href='/dashboard'>overview</a> / queues</p>",
            f"<p><a href='/dashboard/new-campaign?manage_token={_e(token)}'>"
            f"<button>+ start a new campaign</button></a></p>",
            "<h2>Proposals</h2>",
        ]
        for p in props:
            flag = (
                " <span class='state exit'>rebalancing flag</span>"
                if p.kind == "rebalance" else ""
            )
            rows.append(
                f"<div class='card'><b>[{_e(p.kind)}]</b> {_e(p.summary)}{flag} "
                f"<span class='small'>({_e(p.tenant)})</span> "
                + _form(f"/dashboard/proposals/{p.id}/approve", "approve", token)
                + _form(f"/dashboard/proposals/{p.id}/decline", "decline", token,
                        danger=True)
                + "</div>"
            )
        rows.append("<h2>Review ramp</h2>")
        for tid, campaign, subject, body in held:
            rows.append(
                f"<div class='card'><b>{_e(campaign)}</b> — {_e(subject)}"
                f"<pre>{_e(body)}</pre>"
                + _form(f"/dashboard/touches/{tid}/send", "send", token)
                + _form(f"/dashboard/touches/{tid}/reject", "reject", token, danger=True)
                + "</div>"
            )
        rows.append("<h2>Human tasks</h2>")
        for ht in tasks:
            rows.append(
                f"<div class='card'><b>{_e(ht.instruction)}</b>"
                f"<pre>{_e(ht.brief)}</pre>"
                + _form(f"/dashboard/tasks/{ht.id}/done", "done (counts as contact)", token)
                + _form(f"/dashboard/tasks/{ht.id}/skip", "skip", token, danger=True)
                + "</div>"
            )
        rows.append("<h2>Escalations</h2>")
        rows += [
            f"<div class='card'>[{_e(e.subject_type)}] {_e(e.reason)} "
            f"<span class='small'>({_e(e.tenant)})</span></div>"
            for e in escs
        ]
        return _page("Queues & management", "".join(rows))

    def _redirect(token: str) -> RedirectResponse:
        return RedirectResponse(
            f"/dashboard/queues?manage_token={token}", status_code=303
        )

    @router.get("/dashboard/new-campaign", response_class=HTMLResponse)
    def new_campaign_form(request: Request) -> HTMLResponse:
        """FR-9.1 onboarding: the Brief interview as a form. Submitting
        enqueues synthesis; the result lands in the proposals queue."""
        token = _check_manage(request)

        def field(name: str, label: str, hint: str, rows: int = 2,
                  value: str = "") -> str:
            return (
                f"<p><label for='{name}'><b>{_e(label)}</b><br>"
                f"<span class='small'>{_e(hint)}</span></label><br>"
                f"<textarea id='{name}' name='{name}' rows='{rows}' "
                f"style='width:100%;font:inherit;padding:8px'>{_e(value)}</textarea></p>"
            )

        body = (
            f"<p class='crumb'><a href='/dashboard'>overview</a> / "
            f"<a href='/dashboard/queues?manage_token={_e(token)}'>queues</a> "
            f"/ new campaign</p>"
            "<div class='note'><b>How this works</b> You describe the campaign "
            "in plain language; the synthesis agent compiles a full program "
            "(personas, cohorts, generation prompts) and files it as a "
            "<i>proposal</i> for your review — nothing launches without your "
            "approval, and nothing here sends mail.</div>"
            f"<form method='post' "
            f"action='/dashboard/campaigns/new?manage_token={_e(token)}'>"
            "<p><label><b>Campaign id (slug)</b><br>"
            "<span class='small'>lowercase, e.g. nj-providers</span></label><br>"
            "<input name='tenant' pattern='[a-z0-9_-]+' required "
            "style='width:50%;font:inherit;padding:8px'></p>"
            + field("find", "Who should we find?",
                    "What kind of people, where (min 20 chars)", 3)
            + field("research", "What should we research about them?",
                    "What to read on their web presence (min 20 chars)", 3)
            + field("convert", "What counts as a conversion?",
                    "e.g. provider claims their verified profile", 1)
            + field("name", "Your brand name", "shown as the sender's company", 1)
            + field("what_we_do", "What you do (the only source of product claims)",
                    "min 10 chars; the agent can never claim beyond this", 2)
            + field("sender", "Sender identity",
                    'a real person, e.g. "Dana Whitfield, TheraDirectory"', 1)
            + field("physical_address", "Physical mailing address (CAN-SPAM)",
                    "appears in every message footer", 1)
            + field("signup_link", "Signup link (https)", "the conversion CTA", 1)
            + "<p><label><b>Monthly prospect budget</b></label><br>"
              "<input name='monthly_prospects' type='number' value='200' "
              "min='1' style='font:inherit;padding:8px'></p>"
            "<p><button>synthesize program proposal</button></p></form>"
        )
        return _page("Start a new campaign", body)

    @router.post("/dashboard/campaigns/new")
    async def new_campaign_submit(request: Request) -> HTMLResponse:
        token = _check_manage(request)
        form = await request.form()
        brief_dict = {
            "find": str(form.get("find", "")),
            "research": str(form.get("research", "")),
            "goals": {"convert": str(form.get("convert", ""))},
            "about_us": {
                "name": str(form.get("name", "")),
                "what_we_do": str(form.get("what_we_do", "")),
                "links": (
                    {"signup": str(form.get("signup_link", ""))}
                    if form.get("signup_link") else {}
                ),
                "identity": {
                    "sender": str(form.get("sender", "")),
                    "physical_address": str(form.get("physical_address", "")),
                },
            },
            "budgets": {
                "monthly_prospects": int(str(form.get("monthly_prospects", "200"))),
                "monthly_llm_usd": 25,
            },
            "autonomy": "review_everything",
        }
        try:
            brief = Brief.model_validate(brief_dict)
        except Exception as exc:  # pydantic ValidationError -> show, don't 500
            return _page(
                "Start a new campaign",
                f"<div class='note'><b>That brief needs a fix</b>"
                f"<pre>{_e(exc)}</pre></div>"
                f"<p><a href='/dashboard/new-campaign?manage_token={_e(token)}'>"
                f"&larr; back to the form</a></p>",
            )
        tenant = str(form.get("tenant", ""))
        with engine.begin() as conn:
            enqueue_synthesis(conn, tenant, brief)
        return _page(
            "Campaign queued",
            "<div class='note'><b>Synthesis queued</b> The worker will compile "
            "your program and file it as a proposal. Review and approve it in "
            f"<a href='/dashboard/queues?manage_token={_e(token)}'>the queues"
            "</a> (or `reachout approve`); nothing launches before that.</div>",
        )

    @router.post("/dashboard/proposals/{proposal_id}/approve")
    def approve_proposal(proposal_id: str, request: Request) -> RedirectResponse:
        token = _check_manage(request)
        with engine.begin() as conn:
            ok = proposals.approve(conn, proposal_id, actor=ACTOR)
        if not ok:
            raise HTTPException(404, "not an open proposal")
        return _redirect(token)

    @router.post("/dashboard/proposals/{proposal_id}/decline")
    def decline_proposal(proposal_id: str, request: Request) -> RedirectResponse:
        token = _check_manage(request)
        with engine.begin() as conn:
            ok = proposals.decline(conn, proposal_id, actor=ACTOR)
        if not ok:
            raise HTTPException(404, "not an open proposal")
        return _redirect(token)

    @router.post("/dashboard/touches/{touch_id}/send")
    def ramp_send(touch_id: str, request: Request) -> RedirectResponse:
        token = _check_manage(request)
        with engine.begin() as conn:
            ok = sendpath.approve_pending(conn, touch_id, actor=ACTOR)
        if not ok:
            raise HTTPException(404, "not a pending-review touch")
        return _redirect(token)

    @router.post("/dashboard/touches/{touch_id}/reject")
    def ramp_reject(touch_id: str, request: Request) -> RedirectResponse:
        token = _check_manage(request)
        with engine.begin() as conn:
            ok = sendpath.reject_pending(conn, touch_id, actor=ACTOR)
        if not ok:
            raise HTTPException(404, "not a pending-review touch")
        return _redirect(token)

    @router.post("/dashboard/tasks/{task_id}/done")
    def task_done(task_id: str, request: Request) -> RedirectResponse:
        token = _check_manage(request)
        with engine.begin() as conn:
            ok = human_tasks.resolve(conn, task_id, actor=ACTOR, done=True)
        if not ok:
            raise HTTPException(404, "not a pending task")
        return _redirect(token)

    @router.post("/dashboard/tasks/{task_id}/skip")
    def task_skip(task_id: str, request: Request) -> RedirectResponse:
        token = _check_manage(request)
        with engine.begin() as conn:
            ok = human_tasks.resolve(conn, task_id, actor=ACTOR, done=False)
        if not ok:
            raise HTTPException(404, "not a pending task")
        return _redirect(token)

    @router.post("/dashboard/tenants/{tenant}/pause")
    def pause_tenant(tenant: str, request: Request) -> RedirectResponse:
        token = _check_manage(request)
        with engine.begin() as conn:
            control.halt(conn, scope=tenant, actor=ACTOR)
        return _redirect(token)

    @router.post("/dashboard/tenants/{tenant}/resume")
    def resume_tenant(tenant: str, request: Request) -> RedirectResponse:
        token = _check_manage(request)
        with engine.begin() as conn:
            control.resume(conn, scope=tenant, actor=ACTOR)
        return _redirect(token)

    return router
