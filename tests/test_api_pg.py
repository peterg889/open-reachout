"""Operator API + conversion attribution against Postgres (FR-1.6, gate 12)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from tests.conftest import Seed
from tests.test_worker_e2e_pg import make_worker, queue_one

from open_reachout.adapters.fakes import FakeSendingProvider
from open_reachout.api.app import ApiToken, create_app, parse_tokens
from open_reachout.core import attribution

pytestmark = pytest.mark.postgres

KEY = b"attribution-test-key"
TOKENS = [
    ApiToken("ops", "s" * 24, frozenset({"conversions:write", "control:write",
                                         "privacy:write", "events:write", "read"})),
    ApiToken("readonly", "r" * 24, frozenset({"read"})),
]


def client(pg_engine: Engine, provider: FakeSendingProvider | None = None) -> TestClient:
    app = create_app(pg_engine, provider or FakeSendingProvider(),
                     attribution_key=KEY, tokens=TOKENS)
    return TestClient(app, raise_server_exceptions=False)


def auth(secret: str = "s" * 24) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def test_token_roundtrip_and_tamper() -> None:
    touch = "0b6cda1c-9d5e-4cb9-a5e9-1234567890ab"
    token = attribution.token_for(touch, KEY)
    assert attribution.verify(token, KEY) == touch
    assert attribution.verify(token[:-1] + "0", KEY) is None
    assert attribution.verify("garbage", KEY) is None
    assert attribution.verify(token, b"other-key") is None


@pytest.mark.gates
def test_gate12_attribution_round_trip(pg_engine: Engine, conn, seed: Seed) -> None:
    """Signed touch token survives the conversion round-trip; CAC reconciles."""
    conn.commit()
    touch_id = queue_one(pg_engine, seed)
    provider = FakeSendingProvider()
    make_worker(pg_engine, provider).drain()

    api = client(pg_engine, provider)
    token = attribution.token_for(touch_id, KEY)
    first = api.post("/v1/conversions", json={"token": token}, headers=auth())
    assert first.status_code == 200 and first.json()["converted"] is True
    replay = api.post("/v1/conversions", json={"token": token}, headers=auth())
    assert replay.json()["converted"] is False  # idempotent

    with pg_engine.begin() as c:
        assert c.execute(text("SELECT state FROM prospects")).scalar() == "converted"
        # True conversion feeds the bandit (FR-8.3).
        successes = c.execute(
            text("SELECT successes FROM variant_stats WHERE variant_id='v1'")
        ).scalar()
        assert successes == 1

    forged = api.post("/v1/conversions",
                      json={"token": touch_id.replace("-", "") + ".badmac"},
                      headers=auth())
    assert forged.status_code == 401


def test_auth_and_scopes(pg_engine: Engine, conn, seed: Seed) -> None:
    conn.commit()
    api = client(pg_engine)
    assert api.post("/v1/halt", json={}).status_code == 401  # no token
    assert api.post("/v1/halt", json={}, headers=auth("x" * 24)).status_code == 401
    denied = api.post("/v1/halt", json={}, headers=auth("r" * 24))
    assert denied.status_code == 403  # readonly token lacks control:write
    assert api.get("/v1/funnel", headers=auth("r" * 24)).status_code == 200


def test_halt_resume_forget_and_events_via_api(pg_engine: Engine, conn, seed: Seed) -> None:
    conn.commit()
    api = client(pg_engine)
    assert api.post("/v1/halt", json={}, headers=auth()).status_code == 200
    with pg_engine.begin() as c:
        flag = c.execute(text("SELECT set_by FROM control_flags")).scalar()
        assert flag == "operator:ops"  # API tokens are human actors
    assert api.post("/v1/resume", json={}, headers=auth()).json()["resumed"] is True

    gone = api.post("/v1/forget", json={"ref": seed.email}, headers=auth())
    assert gone.status_code == 200 and gone.json()["addresses_tombstoned"] == 1
    missing = api.post("/v1/forget", json={"ref": "nobody@nowhere.test"}, headers=auth())
    assert missing.status_code == 200 or missing.status_code == 404

    ev = {"event_type": "compact_state_issuing",
          "selector": {"state": "TX"}, "payload": {"compact": "counseling"},
          "dedupe_key": "tx-issuing-1"}
    first = api.post("/v1/events", json=ev, headers=auth())
    assert first.status_code == 202 and first.json()["recorded"] is True
    dup = api.post("/v1/events", json=ev, headers=auth())
    assert dup.json()["recorded"] is False  # dedupe_key

    funnel = api.get("/v1/funnel", headers=auth()).json()
    assert funnel["stagematch"]["forgotten"] == 1


@pytest.mark.gates
@pytest.mark.disqualifying
def test_gate13_webhook_endpoint_rejects_forged_signatures(
    pg_engine: Engine, conn, seed: Seed
) -> None:
    conn.commit()
    touch_id = queue_one(pg_engine, seed)
    provider = FakeSendingProvider()
    make_worker(pg_engine, provider).drain()
    api = client(pg_engine, provider)

    raw = json.dumps({"id": "w-1", "kind": "reply",
                      "touch_ref": {"touch_id": touch_id},
                      "payload": {"body": "hi"}}).encode()
    ok = api.post("/hooks/provider", content=raw,
                  headers={"x-or-signature": provider.sign(raw)})
    assert ok.status_code == 200 and ok.json()["processed"] == 1

    forged = api.post("/hooks/provider", content=raw,
                      headers={"x-or-signature": "deadbeef"})
    assert forged.status_code == 401
    with pg_engine.begin() as c:
        alerts = c.execute(
            text("SELECT count(*) FROM audit_events WHERE event='signature_rejected'")
        ).scalar()
        assert alerts == 1


def test_parse_tokens_rejects_weak_secrets() -> None:
    with pytest.raises(ValueError, match="too short"):
        parse_tokens("ops:short:read")
    tokens = parse_tokens("ops:" + "s" * 24 + ":read|control:write")
    assert tokens[0].scopes == {"read", "control:write"}
