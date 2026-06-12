"""Agentic reply composer (FR-4.2/4.3): FAQ-grounded answers queued through
the REPLY gate profile, one exchange ever, objection counter-snippets used,
failures escalate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from tests.conftest import Seed

from open_reachout.core import escalations, events
from open_reachout.core.config import TenantConfig, load_tenant
from open_reachout.core.prospecting import runtime_for
from open_reachout.core.queue import enqueue
from open_reachout.core.worker import Worker

pytestmark = pytest.mark.postgres

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class _ReplyLLM:
    """Classifies per ctor args; composes a compliant reply echoing the FAQ."""

    def __init__(self, intent: str = "question", objection_class: str | None = None,
                 ctx=None) -> None:  # noqa: ANN001
        self.intent, self.objection_class, self.ctx = intent, objection_class, ctx

    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        if task == "classify_reply":
            return schema.model_validate(
                {"intent": self.intent, "confidence": 0.95,
                 "objection_class": self.objection_class}
            )
        assert task == "compose_reply"
        assert "<untrusted" in prompt          # inbound text is enveloped
        snippet = "flat $9/mo" if "flat $9/mo" in prompt else "free for venues"
        body = (
            f"Good question - it's {snippet}, no other charges.\n"
            f"- {self.ctx.sender_identity}\n{self.ctx.physical_address}\n"
            f"{self.ctx.unsubscribe_text}"
        )
        return schema.model_validate({"subject": "About pricing", "body": body})


def _setup(conn: Connection, seed: Seed, *, counters: bool = False):  # noqa: ANN202
    raw = load_tenant(EXAMPLES / "music-marketplace" / "tenant.yaml").model_dump()
    raw["brief"]["about_us"]["faq"] = {
        "what does it cost": "Venue accounts are free; bands pay a flat $9/mo."
    }
    if counters:
        raw["personas"][0]["objection_counters"] = {
            "price": "Bands pay a flat $9/mo; venues never pay."
        }
    cfg = TenantConfig.model_validate(raw)
    runtime = runtime_for(conn, cfg)
    conn.execute(
        text("UPDATE prospects SET persona_id='small_venue', state='engaged' "
             "WHERE id = CAST(:p AS uuid)"),
        {"p": seed.prospect_id},
    )
    return {cfg.tenant: runtime}


def _reply(conn: Connection, seed: Seed, body: str) -> str:
    return str(conn.execute(
        text("INSERT INTO replies (prospect_id, body) VALUES (CAST(:p AS uuid), :b) "
             "RETURNING id"),
        {"p": seed.prospect_id, "b": body},
    ).scalar())


def _drain(pg_engine: Engine, runtimes: dict, llm) -> None:  # noqa: ANN001
    Worker(pg_engine, handlers={
        "classify": events.make_classify_handler(llm, runtimes),
    }).drain()


def test_faq_answer_queued_once_through_reply_profile(
    pg_engine: Engine, conn: Connection, seed: Seed
) -> None:
    runtimes = _setup(conn, seed)
    llm = _ReplyLLM(ctx=next(iter(runtimes.values())).validator_ctx)
    rid = _reply(conn, seed, "What does this cost for a small room like ours?")
    enqueue(conn, "classify", {"reply_id": rid}, idempotency_key=f"classify:{rid}")
    conn.commit()
    _drain(pg_engine, runtimes, llm)
    with pg_engine.begin() as c:
        kind, body, status = c.execute(
            text("""SELECT kind, body, status FROM touches
                    WHERE prospect_id = CAST(:p AS uuid)
                      AND campaign_id LIKE '%agentic_reply'"""),
            {"p": seed.prospect_id},
        ).fetchone()
        assert kind == "agentic_reply" and status == "drafted"
        assert "flat $9/mo" in body            # grounded in the FAQ, not invented
        exchanges = c.execute(
            text("SELECT agentic_exchanges FROM replies WHERE id = CAST(:i AS uuid)"),
            {"i": rid},
        ).scalar()
        assert exchanges == 1
        # second reply on the SAME thread: cap reached -> escalate, no compose
        rid2 = _reply(c, seed, "But what does it cost after the first month?")
        c.execute(text("UPDATE replies SET agentic_exchanges = 1 "
                       "WHERE id = CAST(:i AS uuid)"), {"i": rid2})
        enqueue(c, "classify", {"reply_id": rid2}, idempotency_key=f"classify:{rid2}")
    _drain(pg_engine, runtimes, llm)
    with pg_engine.begin() as c:
        n_replies = c.execute(
            text("""SELECT count(*) FROM touches WHERE prospect_id = CAST(:p AS uuid)
                    AND kind = 'agentic_reply'"""),
            {"p": seed.prospect_id},
        ).scalar()
        assert n_replies == 1                  # one exchange, ever (FR-4.2)
        assert [e for e in escalations.list_open(c)
                if "exchange cap" in e.reason]


def test_objection_uses_operator_counter_snippet(
    pg_engine: Engine, conn: Connection, seed: Seed
) -> None:
    runtimes = _setup(conn, seed, counters=True)
    llm = _ReplyLLM(intent="objection", objection_class="price",
                    ctx=next(iter(runtimes.values())).validator_ctx)
    rid = _reply(conn, seed, "Too pricey for us I think.")
    enqueue(conn, "classify", {"reply_id": rid}, idempotency_key=f"classify:{rid}")
    conn.commit()
    _drain(pg_engine, runtimes, llm)
    with pg_engine.begin() as c:
        body = c.execute(
            text("""SELECT body FROM touches WHERE prospect_id = CAST(:p AS uuid)
                    AND kind = 'agentic_reply'"""),
            {"p": seed.prospect_id},
        ).scalar()
        assert "flat $9/mo" in body
        klass = c.execute(text("SELECT class FROM objections")).scalar()
        assert klass == "price"               # taxonomy recorded alongside
