"""Operator REST API (PRD FR-1.6, spec 11): the integration points an
operator's own systems call. Bearer tokens are scoped and constant-time
compared; the CLI and this API are both thin shells over the same core.

Token format (OR_API_TOKENS): `name:secret:scope1|scope2,name2:...`
Scopes: events:write, conversions:write, privacy:write, control:write, read.
"""

from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Engine

from open_reachout.core import attribution, control, events, forget
from open_reachout.core import queue as job_queue
from open_reachout.core.attribution import ATTRIBUTION_KEY_ENV
from open_reachout.core.interfaces import SendingProvider, WebhookVerificationError

TOKENS_ENV = "OR_API_TOKENS"


@dataclass(frozen=True)
class ApiToken:
    name: str
    secret: str
    scopes: frozenset[str]


def parse_tokens(raw: str) -> list[ApiToken]:
    tokens = []
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        name, secret, scopes = entry.split(":", 2)
        if len(secret) < 16:
            raise ValueError(f"API token {name!r} secret is too short (min 16 chars)")
        tokens.append(ApiToken(name, secret, frozenset(scopes.split("|"))))
    return tokens


class ConversionIn(BaseModel):
    token: str


class ForgetIn(BaseModel):
    ref: str


class ControlIn(BaseModel):
    scope: str = "global"


class OperatorEventIn(BaseModel):
    event_type: str = Field(min_length=1)
    selector: dict = Field(default_factory=dict)
    payload: dict = Field(default_factory=dict)
    dedupe_key: str | None = None


class ProgramIn(BaseModel):
    tenant: str = Field(pattern=r"^[a-z0-9_-]+$")
    brief: dict[str, object]


def create_app(
    engine: Engine,
    provider: SendingProvider,
    *,
    attribution_key: bytes | None = None,
    tokens: list[ApiToken] | None = None,
) -> FastAPI:
    key = attribution_key or os.environ.get(ATTRIBUTION_KEY_ENV, "").encode()
    if not key:
        raise RuntimeError(f"{ATTRIBUTION_KEY_ENV} is not set")
    api_tokens = tokens if tokens is not None else parse_tokens(
        os.environ.get(TOKENS_ENV, "")
    )

    app = FastAPI(title="Open Reachout", docs_url=None, redoc_url=None)

    from open_reachout.api.dashboard import build_dashboard_router
    from open_reachout.api.manage import build_manage_router

    app.include_router(build_dashboard_router(engine))
    app.include_router(build_manage_router(engine))  # FR-9.1/9.4 (manage:write)

    def require(scope: str):
        def check(authorization: str = Header(default="")) -> ApiToken:
            secret = authorization.removeprefix("Bearer ").strip()
            for token in api_tokens:
                if hmac.compare_digest(token.secret, secret):
                    if scope not in token.scopes:
                        raise HTTPException(403, f"token lacks scope {scope!r}")
                    return token
            raise HTTPException(401, "invalid token")

        return Depends(check)

    @app.post("/v1/conversions", status_code=200)
    def conversions(
        body: ConversionIn, token: ApiToken = require("conversions:write")
    ) -> dict:
        touch_id = attribution.verify(body.token, key)
        if touch_id is None:
            # No unauthenticated state changes (FR-8.3) — and no oracle.
            raise HTTPException(401, "invalid attribution token")
        with engine.begin() as conn:
            converted = attribution.record_conversion(conn, touch_id)
        return {"converted": converted, "touch_id": touch_id}

    @app.post("/v1/forget", status_code=200)
    def forget_route(body: ForgetIn, token: ApiToken = require("privacy:write")) -> dict:
        with engine.begin() as conn:
            try:
                receipt = forget.forget(conn, body.ref)
            except forget.UnknownSubjectError as exc:
                raise HTTPException(404, str(exc)) from exc
        return {"receipt_id": receipt.receipt_id,
                "addresses_tombstoned": receipt.addresses_tombstoned}

    @app.post("/v1/halt", status_code=200)
    def halt_route(body: ControlIn, token: ApiToken = require("control:write")) -> dict:
        with engine.begin() as conn:
            control.halt(conn, scope=body.scope, actor=f"operator:{token.name}")
        return {"halted": body.scope}

    @app.post("/v1/resume", status_code=200)
    def resume_route(body: ControlIn, token: ApiToken = require("control:write")) -> dict:
        with engine.begin() as conn:
            cleared = control.resume(conn, scope=body.scope, actor=f"operator:{token.name}")
        return {"resumed": cleared}

    @app.post("/v1/events", status_code=202)
    def operator_events(
        body: OperatorEventIn, token: ApiToken = require("events:write")
    ) -> dict:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO operator_events (event_type, selector, payload, dedupe_key)
                    VALUES (:e, CAST(:s AS jsonb), CAST(:p AS jsonb), :k)
                    ON CONFLICT (dedupe_key) DO NOTHING
                    RETURNING id
                    """
                ),
                {"e": body.event_type, "s": json.dumps(body.selector),
                 "p": json.dumps(body.payload), "k": body.dedupe_key},
            ).fetchone()
            if row is not None:
                # FR-2.9: hand off to the trigger queue; the worker matches
                # `trigger: event` cohorts and starts sequences through the
                # full gate set. Duplicate events (dedupe_key) never re-fire.
                job_queue.enqueue(
                    conn, "trigger", {"event_id": str(row[0])},
                    idempotency_key=f"trigger:{row[0]}",
                )
        return {"recorded": row is not None, "id": str(row[0]) if row else None}

    @app.post("/v1/programs", status_code=202)
    def programs(
        body: ProgramIn, token: ApiToken = require("manage:write")
    ) -> dict[str, object]:
        """FR-9.1: Brief in -> synthesis job -> Program Proposal. The Brief is
        schema-validated HERE (fail fast); the worker runs the LLM synthesis
        and files the proposal for human approval."""
        from pydantic import ValidationError

        from open_reachout.core.config import Brief
        from open_reachout.core.programs import enqueue_synthesis

        try:
            brief = Brief.model_validate(body.brief)
        except ValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        with engine.begin() as conn:
            digest = enqueue_synthesis(conn, body.tenant, brief)
        return {"queued": True, "brief_hash": digest}

    @app.get("/v1/funnel")
    def funnel(token: ApiToken = require("read")) -> dict:
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT t.slug, p.state, count(*) FROM prospects p
                    JOIN tenants t ON t.id = p.tenant_id GROUP BY 1, 2
                    """
                )
            ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for tenant, state, n in rows:
            out.setdefault(tenant, {})[state] = n
        return out

    @app.post("/hooks/provider", status_code=200)
    async def provider_hook(request: Request) -> dict:
        payload = await request.body()
        signature = request.headers.get("x-or-signature", "")
        try:
            with engine.begin() as conn:
                processed = events.ingest_webhook(conn, provider, payload, signature)
        except WebhookVerificationError as exc:
            # Drop + alert (gate 13): unsigned events never reach processing.
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO audit_events (subject_type, subject_id, event,
                            payload, actor)
                        VALUES ('webhook', 'provider', 'signature_rejected',
                            '{}'::jsonb, 'system:api')
                        """
                    )
                )
            raise HTTPException(401, "invalid signature") from exc
        return {"processed": processed}

    return app
