"""Production hallucination monitor (FR-8.6): sampled re-judging of sent mail,
per-cohort grounded rate recorded, sub-threshold rates escalate.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Connection
from tests.conftest import Seed

from open_reachout.core import escalations, monitor

pytestmark = pytest.mark.postgres


def _sent_touch(conn: Connection, seed: Seed, body: str) -> str:
    tid = str(uuid.uuid4())
    conn.execute(
        text(
            """INSERT INTO touches (id, prospect_id, campaign_id, kind, status,
                   subject, body, content_hash, idempotency_key, sent_at)
               VALUES (CAST(:i AS uuid), CAST(:p AS uuid), 'c', 'cold', 'sent',
                   's', :b, 'h', :i, now())"""
        ),
        {"i": tid, "p": seed.prospect_id, "b": body},
    )
    return tid


class _JudgeLLM:
    """Grounded unless the body smells invented; counts calls."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        assert task == "groundedness"
        self.calls += 1
        bad = "they host weekly jazz nights" in prompt  # not in any evidence
        return schema.model_validate(
            {"grounded": not bad,
             "unsupported_claims": (["weekly jazz nights"] if bad else [])}
        )


def test_clean_sample_records_full_rate(conn: Connection, seed: Seed) -> None:
    for _ in range(3):
        _sent_touch(conn, seed, "Saw your calendar - nice Friday series.")
    llm = _JudgeLLM()
    results = monitor.audit_sent_sample(conn, llm, per_cohort=5)
    key = f"{seed.tenant}/austin_venues"
    assert results[key]["rate"] == 1.0 and results[key]["n"] == 3
    assert llm.calls == 3
    recorded = conn.execute(
        text(
            """SELECT payload->>'rate' FROM audit_events
               WHERE event = 'groundedness_audit' AND subject_id = 'austin_venues'"""
        )
    ).scalar()
    assert recorded == "1.0"
    assert not [e for e in escalations.list_open(conn) if e.subject_id == "austin_venues"]


def test_ungrounded_sends_escalate(conn: Connection, seed: Seed) -> None:
    _sent_touch(conn, seed, "Loved that they host weekly jazz nights at your bar.")
    _sent_touch(conn, seed, "Saw your calendar - nice Friday series.")
    results = monitor.audit_sent_sample(conn, llm=_JudgeLLM(), per_cohort=5)
    key = f"{seed.tenant}/austin_venues"
    assert results[key]["failures"] == 1
    (esc,) = [e for e in escalations.list_open(conn) if e.subject_id == "austin_venues"]
    assert "groundedness rate 50%" in esc.reason


def test_sample_respects_per_cohort_cap_and_window(conn: Connection, seed: Seed) -> None:
    for i in range(8):
        _sent_touch(conn, seed, f"note {i}")
    old = _sent_touch(conn, seed, "ancient send")
    conn.execute(
        text("UPDATE touches SET sent_at = now() - interval '30 days' "
             "WHERE id = CAST(:i AS uuid)"),
        {"i": old},
    )
    llm = _JudgeLLM()
    results = monitor.audit_sent_sample(conn, llm, per_cohort=4)
    assert results[f"{seed.tenant}/austin_venues"]["n"] == 4  # capped, window-bound
    assert llm.calls == 4
