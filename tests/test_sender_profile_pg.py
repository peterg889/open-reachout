"""Sender-profile research (FR-0.7, spec 8.10): proposed by the agent, trusted
only after one-time human approval — the only path by which researched content
becomes trusted-class {{sender.*}} variables.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Connection
from tests.conftest import Seed

from open_reachout.core import sender_profile
from open_reachout.core.config import load_tenant
from open_reachout.core.prospecting import runtime_for
from open_reachout.core.variables import TrustClass

pytestmark = pytest.mark.postgres

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

FACTS = [
    {"slug": "years_booking", "fact": "Eight years booking rooms in Austin",
     "source_url": "https://stagematch.test/about"},
    {"slug": "scene_role", "fact": "Runs the Red River venue association meetup",
     "source_url": ""},
]


class _ResearchLLM:
    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        assert task == "sender_research"
        return schema.model_validate({"facts": FACTS})


def _config():  # noqa: ANN202
    return load_tenant(EXAMPLES / "music-marketplace" / "tenant.yaml")


def test_propose_approve_elevates_to_trusted(conn: Connection, seed: Seed) -> None:
    cfg = _config()
    pid = sender_profile.propose(conn, _ResearchLLM(), cfg)
    # proposed facts are NOT live
    assert sender_profile.approved_facts(conn, cfg.tenant) == {}
    with pytest.raises(PermissionError, match="human"):
        sender_profile.approve(conn, pid, actor="system:research")
    assert sender_profile.approve(conn, pid, actor="operator:cli")
    facts = sender_profile.approved_facts(conn, cfg.tenant)
    assert facts["years_booking"].startswith("Eight years")
    # runtime threads them in as trusted-class variables
    runtime = runtime_for(conn, cfg)
    assert runtime.sender_facts == facts
    from open_reachout.core import dryrun
    from open_reachout.core.interfaces import Candidate, DataBasis, EvidenceCard

    candidate = Candidate(display_name="Sam Venue", org_name="Cactus Cafe",
                          email_raw="sam@venue.test", source_adapter="fake",
                          source_ref={}, data_basis=DataBasis.GOVERNMENT_PUBLIC)
    values = dryrun.build_values(
        "Mention {{sender.years_booking}} to {{prospect.first_name}}. "
        "{{persona.voice_rules}}",
        candidate, EvidenceCard(prospect_ref="x", facts=[]),
        cfg, cfg.personas[0], sender_facts=runtime.sender_facts,
    )
    assert values["sender.years_booking"].trust is TrustClass.TRUSTED
    assert values["sender.years_booking"].value.startswith("Eight years")


def test_reapproval_supersedes_old_profile(conn: Connection, seed: Seed) -> None:
    cfg = _config()
    first = sender_profile.propose(conn, _ResearchLLM(), cfg)
    sender_profile.approve(conn, first, actor="operator:cli")
    second = sender_profile.propose(conn, _ResearchLLM(), cfg)
    sender_profile.approve(conn, second, actor="operator:cli")
    statuses = dict(conn.execute(
        text("SELECT id::text, status FROM sender_profiles WHERE tenant = :t"),
        {"t": cfg.tenant},
    ).fetchall())
    assert statuses[first] == "superseded"
    assert statuses[second] == "approved"
    # double-approve of a resolved profile is a no-op
    assert sender_profile.approve(conn, first, actor="operator:cli") is False


def test_unapproved_sender_slot_fails_closed(conn: Connection, seed: Seed) -> None:
    from open_reachout.core import dryrun
    from open_reachout.core.interfaces import Candidate, DataBasis, EvidenceCard
    from open_reachout.core.variables import resolve

    cfg = _config()
    candidate = Candidate(display_name="Sam Venue", org_name="Cactus Cafe",
                          email_raw="s@v.test", source_adapter="fake",
                          source_ref={}, data_basis=DataBasis.GOVERNMENT_PUBLIC)
    prompt = "Mention {{sender.years_booking}}. {{persona.voice_rules}}"
    values = dryrun.build_values(
        prompt, candidate, EvidenceCard(prospect_ref="x", facts=[]),
        cfg, cfg.personas[0], sender_facts={},
    )
    with pytest.raises(KeyError):
        resolve(prompt, values)  # missing approval -> escalation upstream
