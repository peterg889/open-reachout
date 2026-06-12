"""End-to-end live path against Postgres: queue_draft -> worker -> provider,
webhooks -> deterministic handling -> classify -> routing (spec 7.4/8.5)."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Engine
from tests.conftest import Seed
from tests.test_pg_gates import BODY, CTX

from open_reachout.adapters.fakes import FakeSendingProvider
from open_reachout.core import control, events, forget, sendpath, suppression
from open_reachout.core.compliance.validators import Draft, content_hash
from open_reachout.core.interfaces import WebhookVerificationError
from open_reachout.core.worker import Worker

pytestmark = pytest.mark.postgres


class StubLLM:
    def __init__(self, intent: str = "interested", confidence: float = 0.95) -> None:
        self.intent, self.confidence = intent, confidence

    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        return schema.model_validate({"intent": self.intent, "confidence": self.confidence})


def make_worker(
    pg_engine: Engine, provider: FakeSendingProvider, llm: StubLLM | None = None
) -> Worker:
    return Worker(
        pg_engine,
        handlers={
            "control": events.make_control_handler(provider),
            "classify": events.make_classify_handler(llm or StubLLM()),
            "deliver": sendpath.make_deliver_handler(provider, {"stagematch": CTX}),
        },
    )


def queue_one(pg_engine: Engine, seed: Seed) -> str:
    draft = Draft(subject="About Thursday nights", body=BODY)
    with pg_engine.begin() as c:
        return sendpath.queue_draft(
            c, prospect_id=seed.prospect_id, campaign_id="camp-1", variant_id="v1",
            step_index=0, kind="cold", draft=draft, content_hash=content_hash(draft),
        )


def test_e2e_draft_to_dispatched(pg_engine: Engine, conn, seed: Seed) -> None:
    conn.commit()  # release the seed txn so the worker sees it
    touch_id = queue_one(pg_engine, seed)
    provider = FakeSendingProvider()
    assert make_worker(pg_engine, provider).drain() == 1
    assert len(provider.sent) == 1 and provider.sent[0][0].touch_id == touch_id
    with pg_engine.begin() as c:
        status, mailbox = c.execute(
            text("SELECT status, mailbox FROM touches WHERE id = CAST(:i AS uuid)"),
            {"i": touch_id},
        ).fetchone()
        assert status == "dispatched" and mailbox == "mb1@try-stagematch.com"
        assert c.execute(text("SELECT state FROM prospects")).scalar() == "contacted"
        job = c.execute(text("SELECT status FROM jobs WHERE queue='deliver'")).scalar()
        assert job == "done"


@pytest.mark.gates
@pytest.mark.disqualifying
def test_gate03_suppression_between_draft_and_deliver(
    pg_engine: Engine, conn, seed: Seed
) -> None:
    """Reactive enforcement: an opt-out landing after compose releases the
    touch instead of sending (spec 7.6)."""
    conn.commit()
    touch_id = queue_one(pg_engine, seed)
    provider = FakeSendingProvider()
    with pg_engine.begin() as c:
        suppression.suppress(c, seed.email, reason="unsubscribe")
    make_worker(pg_engine, provider).drain()
    assert provider.sent == []
    # The propagation job also ran: provider lead paused.
    assert provider.paused_leads == [seed.email]
    with pg_engine.begin() as c:
        status = c.execute(
            text("SELECT status FROM touches WHERE id = CAST(:i AS uuid)"), {"i": touch_id}
        ).scalar()
        assert status == "released"


@pytest.mark.gates
@pytest.mark.disqualifying
def test_gate04_halt_pauses_dispatch_and_provider(pg_engine: Engine, conn, seed: Seed) -> None:
    conn.commit()
    queue_one(pg_engine, seed)
    provider = FakeSendingProvider()
    with pg_engine.begin() as c:
        control.halt(c, actor="operator:cli")
    make_worker(pg_engine, provider).drain()
    assert provider.sent == []
    assert provider.paused_tenants == ["global"]  # fan-out happened
    with pg_engine.begin() as c:
        # Deliver job retried-with-backoff, not dead: it resumes after resume.
        status = c.execute(text("SELECT status FROM jobs WHERE queue='deliver'")).scalar()
        assert status == "ready"


def test_provider_failure_rolls_back_claim_atomically(
    pg_engine: Engine, conn, seed: Seed
) -> None:
    conn.commit()
    queue_one(pg_engine, seed)

    class FlakyProvider(FakeSendingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next = True

        def send(self, message, subject, body):  # type: ignore[override]
            if self.fail_next:
                self.fail_next = False
                raise ConnectionError("provider 503")
            return super().send(message, subject, body)

    provider = FlakyProvider()
    make_worker(pg_engine, provider).drain()
    with pg_engine.begin() as c:
        # First attempt failed AFTER the claim: rollback restored everything.
        status = c.execute(text("SELECT status FROM touches")).scalar()
        assert status == "drafted"
        assert c.execute(text("SELECT coalesce(sum(used),0) FROM counters")).scalar() == 0
        # Retry is scheduled with backoff…
        c.execute(text("UPDATE jobs SET run_after = now() WHERE queue='deliver'"))
    make_worker(pg_engine, provider).drain()
    assert len(provider.sent) == 1  # …and succeeds cleanly the second time.


def test_webhook_bounce_reply_dedupe_and_routing(pg_engine: Engine, conn, seed: Seed) -> None:
    conn.commit()
    touch_id = queue_one(pg_engine, seed)
    provider = FakeSendingProvider()
    make_worker(pg_engine, provider).drain()

    def hook(payload: dict) -> int:
        raw = json.dumps(payload).encode()
        with pg_engine.begin() as c:
            return events.ingest_webhook(c, provider, raw, provider.sign(raw))

    # Reply -> classify queued, prospect engaged.
    reply_event = {"id": "ev-1", "kind": "reply",
                   "touch_ref": {"touch_id": touch_id},
                   "payload": {"body": "Sounds interesting, how does pricing work?"}}
    assert hook(reply_event) == 1
    assert hook(reply_event) == 0  # duplicate delivery: no-op (I-10)
    make_worker(pg_engine, provider, StubLLM("question")).drain()
    with pg_engine.begin() as c:
        assert c.execute(text("SELECT state FROM prospects")).scalar() == "engaged"
        intent = c.execute(text("SELECT intent FROM replies")).scalar()
        assert intent == "question"

    # Bounce on the same touch: deterministic suppression, no LLM involved.
    assert hook({"id": "ev-2", "kind": "bounce",
                 "touch_ref": {"touch_id": touch_id}, "payload": {}}) == 1
    with pg_engine.begin() as c:
        assert suppression.is_suppressed(c, seed.email, seed.tenant)

    # Bad signature never reaches processing (gate 13).
    raw = json.dumps({"id": "ev-3", "kind": "reply"}).encode()
    with pytest.raises(WebhookVerificationError):
        with pg_engine.begin() as c:
            events.ingest_webhook(c, provider, raw, "forged")


def test_unsubscribe_reply_routes_deterministically(pg_engine: Engine, conn, seed: Seed) -> None:
    conn.commit()
    touch_id = queue_one(pg_engine, seed)
    provider = FakeSendingProvider()

    class ExplodingLLM:
        def complete(self, *a: object) -> BaseModel:
            raise AssertionError("opt-out must not consult the LLM (I-11)")

    worker = Worker(
        pg_engine,
        handlers={
            "control": events.make_control_handler(provider),
            "classify": events.make_classify_handler(ExplodingLLM()),  # type: ignore[arg-type]
            "deliver": sendpath.make_deliver_handler(provider, {"stagematch": CTX}),
        },
    )
    worker.drain()
    raw = json.dumps({"id": "ev-9", "kind": "reply",
                      "touch_ref": {"touch_id": touch_id},
                      "payload": {"body": "please remove me from your list"}}).encode()
    with pg_engine.begin() as c:
        events.ingest_webhook(c, provider, raw, provider.sign(raw))
    worker.drain()
    with pg_engine.begin() as c:
        assert c.execute(text("SELECT state FROM prospects")).scalar() == "unsubscribed"
        assert suppression.is_suppressed(c, seed.email, seed.tenant)


@pytest.mark.gates
@pytest.mark.disqualifying
def test_gate05_forget_propagates_to_provider(pg_engine: Engine, conn, seed: Seed) -> None:
    conn.commit()
    provider = FakeSendingProvider()
    with pg_engine.begin() as c:
        receipt = forget.forget(c, seed.email)
    make_worker(pg_engine, provider).drain()
    assert seed.email in provider.paused_leads
    with pg_engine.begin() as c:
        propagated = c.execute(
            text(
                """
                SELECT provider_propagated_at FROM forget_tombstones
                WHERE receipt_id = CAST(:r AS uuid)
                """
            ),
            {"r": receipt.receipt_id},
        ).scalar()
        assert propagated is not None
