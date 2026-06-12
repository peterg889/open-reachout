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
    style = "background:#fee" if danger else ""
    return (
        f"<form method='post' action='{_e(action)}?manage_token={_e(token)}' "
        f"style='display:inline'><button style='{style}'>{_e(label)}</button></form>"
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
        rows: list[str] = ["<h2>Proposals</h2>"]
        for p in props:
            flag = (
                " <strong style='color:#b00'>[rebalancing flag]</strong>"
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
