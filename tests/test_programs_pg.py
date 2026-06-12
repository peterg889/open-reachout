"""POST /v1/programs (FR-9.1): Brief in -> synthesize job -> Program Proposal
for human approval. Validation fails fast; synthesis failures escalate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from tests.conftest import Seed

from open_reachout.adapters.fakes import FakeSendingProvider
from open_reachout.api.app import ApiToken, create_app
from open_reachout.core import programs, proposals
from open_reachout.core.config import load_tenant
from open_reachout.core.worker import Worker

pytestmark = pytest.mark.postgres

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
TOKENS = [ApiToken("mgr", "m" * 24, frozenset({"manage:write"})),
          ApiToken("ro", "r" * 24, frozenset({"read"}))]

PERSONAS = [{
    "id": "small_venue",
    "description": "Independent cafes and breweries that host live music.",
    "evidence_signals": ["has_events_calendar"],
    "value_prop": "free curated local acts",
    "sequence": {"steps": 2, "gaps_days": [4]},
    "cohorts": [{"id": "austin", "filters": {}, "monthly_budget": 100,
                 "sources": ["google_places"]}],
    "variants": [{"id": "opener_v1", "surface": "opener",
                  "attributes": {"tone": "warm"},
                  "prompt": "Write to {{prospect.first_name}} about "
                            "{{evidence.calendar_highlight}}. {{persona.voice_rules}}"}],
}]


class _SynthLLM:
    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        assert task == "synthesize_program"
        return schema.model_validate({"personas": PERSONAS})


def _client(pg_engine: Engine) -> TestClient:
    app = create_app(pg_engine, FakeSendingProvider(),
                     attribution_key=b"k" * 16, tokens=TOKENS)
    return TestClient(app, raise_server_exceptions=False)


def _brief() -> dict:
    return load_tenant(
        EXAMPLES / "music-marketplace" / "tenant.yaml"
    ).brief.model_dump(mode="json")


def test_brief_to_program_proposal_end_to_end(
    pg_engine: Engine, conn: Connection, seed: Seed
) -> None:
    conn.commit()
    api = _client(pg_engine)
    headers = {"Authorization": "Bearer " + "m" * 24}
    resp = api.post("/v1/programs", json={"tenant": "newbrand", "brief": _brief()},
                    headers=headers)
    assert resp.status_code == 202 and resp.json()["queued"] is True
    # idempotent: same brief, no duplicate job
    api.post("/v1/programs", json={"tenant": "newbrand", "brief": _brief()},
             headers=headers)
    with pg_engine.begin() as c:
        n_jobs = c.execute(
            text("SELECT count(*) FROM jobs WHERE queue = 'synthesize'")
        ).scalar()
        assert n_jobs == 1
    Worker(pg_engine, handlers={
        "synthesize": programs.make_synthesize_handler(_SynthLLM()),
    }).drain()
    with pg_engine.begin() as c:
        (prop,) = [p for p in proposals.list_open(c, "newbrand") if p.kind == "program"]
        assert prop.payload["personas"][0]["id"] == "small_venue"
        assert "reachout init --from-brief" in prop.summary
        # program proposals are always-human (value-prop level)
        with pytest.raises(PermissionError, match="always-human"):
            proposals.approve(c, prop.id, actor="system:x", auto=True)


def test_invalid_brief_fails_fast_and_scopes_bind(pg_engine: Engine, conn, seed) -> None:  # noqa: ANN001
    conn.commit()
    api = _client(pg_engine)
    bad = {"tenant": "x", "brief": {"find": "too short"}}
    resp = api.post("/v1/programs", json=bad,
                    headers={"Authorization": "Bearer " + "m" * 24})
    assert resp.status_code == 422
    resp = api.post("/v1/programs", json={"tenant": "x", "brief": _brief()},
                    headers={"Authorization": "Bearer " + "r" * 24})
    assert resp.status_code == 403  # read token lacks manage:write
