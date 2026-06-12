"""Discovery agent + proposal flow against Postgres (FR-6.1/6.2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from tests.conftest import Seed

from open_reachout.agents import discovery
from open_reachout.core import proposals

pytestmark = pytest.mark.postgres


def _add_cohort_prospects(
    conn: Connection, seed: Seed, cohort: str, *, contacted: int, converted: int
) -> None:
    """Seed `contacted` prospects in `cohort`, of which `converted` converted."""
    period = datetime.now(UTC).strftime("%Y-%m")
    conn.execute(
        text(
            """
            INSERT INTO counters (scope_type, scope_id, period, used, cap)
            VALUES ('cohort_month', :s, :p, 0, 100)
            ON CONFLICT DO NOTHING
            """
        ),
        {"s": cohort, "p": period},
    )
    for i in range(contacted):
        eid, pid = str(uuid.uuid4()), str(uuid.uuid4())
        conn.execute(
            text(
                """INSERT INTO entities (id, tenant_id) VALUES (CAST(:e AS uuid),
                   CAST(:t AS uuid))"""
            ),
            {"e": eid, "t": seed.tenant_id},
        )
        state = "converted" if i < converted else "contacted"
        conn.execute(
            text(
                """INSERT INTO prospects (id, tenant_id, entity_id, cohort_id, persona_id,
                       state, source_adapter, data_basis)
                   VALUES (CAST(:p AS uuid), CAST(:t AS uuid), CAST(:e AS uuid), :c,
                       'x', :st, 'google_places', 'api_terms')"""
            ),
            {"p": pid, "t": seed.tenant_id, "e": eid, "c": cohort, "st": state},
        )


def test_proposes_budget_shift_from_loser_to_winner(conn: Connection, seed: Seed) -> None:
    _add_cohort_prospects(conn, seed, "winners", contacted=40, converted=8)   # 20%
    _add_cohort_prospects(conn, seed, "losers", contacted=40, converted=1)    # 2.5%

    ids = discovery.analyze(conn, seed.tenant)
    assert ids

    open_props = proposals.list_open(conn, seed.tenant)
    shift = next(p for p in open_props if p.kind == "budget_shift")
    assert shift.payload["from_cohort"] == "losers"
    assert shift.payload["to_cohort"] == "winners"
    assert shift.payload["amount"] == 25  # 25% of the loser's cap of 100


def test_insufficient_signal_proposes_nothing(conn: Connection, seed: Seed) -> None:
    _add_cohort_prospects(conn, seed, "tiny_a", contacted=5, converted=1)
    _add_cohort_prospects(conn, seed, "tiny_b", contacted=5, converted=0)
    assert discovery.analyze(conn, seed.tenant) == []


def test_zero_conversion_cohort_flagged_as_opportunity(conn: Connection, seed: Seed) -> None:
    _add_cohort_prospects(conn, seed, "good", contacted=40, converted=6)
    _add_cohort_prospects(conn, seed, "dead", contacted=45, converted=0)
    discovery.analyze(conn, seed.tenant)
    kinds = {p.kind for p in proposals.list_open(conn, seed.tenant)}
    assert "opportunity" in kinds


def test_declined_proposal_not_repitched_within_memory_window(
    conn: Connection, seed: Seed
) -> None:
    _add_cohort_prospects(conn, seed, "winners", contacted=40, converted=8)
    _add_cohort_prospects(conn, seed, "losers", contacted=40, converted=1)
    discovery.analyze(conn, seed.tenant)
    shift = next(p for p in proposals.list_open(conn, seed.tenant) if p.kind == "budget_shift")
    assert proposals.decline(conn, shift.id, actor="operator:cli", note="seasonal")

    # Same analysis again: the declined direction is suppressed.
    discovery.analyze(conn, seed.tenant)
    assert not any(
        p.kind == "budget_shift" for p in proposals.list_open(conn, seed.tenant)
    )


def test_approve_budget_shift_moves_cohort_caps(conn: Connection, seed: Seed) -> None:
    _add_cohort_prospects(conn, seed, "winners", contacted=40, converted=8)
    _add_cohort_prospects(conn, seed, "losers", contacted=40, converted=1)
    discovery.analyze(conn, seed.tenant)
    shift = next(p for p in proposals.list_open(conn, seed.tenant) if p.kind == "budget_shift")

    # System actors cannot approve (mirrors halt-resume / escalations).
    with pytest.raises(PermissionError):
        proposals.approve(conn, shift.id, actor="system:discovery")
    assert proposals.approve(conn, shift.id, actor="operator:cli")

    period = datetime.now(UTC).strftime("%Y-%m")
    caps = dict(
        conn.execute(
            text(
                """
                SELECT scope_id, cap FROM counters
                WHERE scope_type='cohort_month' AND period=:p
                  AND scope_id IN ('winners','losers')
                """
            ),
            {"p": period},
        ).fetchall()
    )
    assert caps == {"winners": 125, "losers": 75}  # +/- 25
    # Re-approval is a no-op (already resolved).
    assert proposals.approve(conn, shift.id, actor="operator:cli") is False


def test_auto_apply_only_for_budget_shift(conn: Connection, seed: Seed) -> None:
    _add_cohort_prospects(conn, seed, "good", contacted=40, converted=6)
    _add_cohort_prospects(conn, seed, "dead", contacted=45, converted=0)
    discovery.analyze(conn, seed.tenant)
    opp = next(p for p in proposals.list_open(conn, seed.tenant) if p.kind == "opportunity")
    # hands_off auto path refuses always-human kinds (FR-0.3).
    with pytest.raises(PermissionError, match="always-human"):
        proposals.approve(conn, opp.id, actor="system:discovery", auto=True)


# ----------------------------------------------------- goal brainstorming (FR-0.5)
from pathlib import Path  # noqa: E402

from pydantic import BaseModel  # noqa: E402

from open_reachout.core.config import load_tenant  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

IDEAS = [
    {"kind": "seasonal_push", "slug": "patio_season", "summary": "Patio-season venue push",
     "rationale": "winners cohort converts; spring uplift", "program_delta":
     "add cohort patio_venues_q2 with monthly_budget 50"},
    {"kind": "adjacent_audience", "slug": "wineries", "summary": "Winery tasting rooms",
     "rationale": "same booking motion as winners", "program_delta":
     "extend filters.categories with winery"},
]


class _StubLLM:
    """Returns scripted brainstorm ideas; counts calls."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        assert task == "brainstorm_goals"
        self.calls += 1
        return schema.model_validate({"ideas": IDEAS})


def _config():  # noqa: ANN202
    return load_tenant(EXAMPLES / "music-marketplace" / "tenant.yaml")


def test_brainstorm_records_and_dedupes(conn: Connection, seed: Seed) -> None:
    llm = _StubLLM()
    first = discovery.brainstorm(conn, llm, _config())
    assert len(first) == 2
    kinds = {p.kind for p in proposals.list_open(conn, seed.tenant)}
    assert "goal_brainstorm" in kinds
    # second run: same slugs are open duplicates -> suppressed
    assert discovery.brainstorm(conn, llm, _config()) == []
    assert llm.calls == 2


def test_brainstorm_respects_decline_memory(conn: Connection, seed: Seed) -> None:
    ids = discovery.brainstorm(conn, _StubLLM(), _config())
    for pid in ids:
        proposals.decline(conn, pid, actor="operator:cli")
    # declined directions are remembered (FR-6.1/0.5): nothing re-pitched
    assert discovery.brainstorm(conn, _StubLLM(), _config()) == []


def test_brainstorm_requires_directive_and_stays_human(conn: Connection, seed: Seed) -> None:
    cfg = _config()
    raw = cfg.model_dump()
    raw["brief"]["goals"]["brainstorm"] = None
    from open_reachout.core.config import TenantConfig

    llm = _StubLLM()
    assert discovery.brainstorm(conn, llm, TenantConfig.model_validate(raw)) == []
    assert llm.calls == 0  # no directive -> no model spend

    ids = discovery.brainstorm(conn, llm, cfg)
    with pytest.raises(PermissionError, match="always-human"):
        proposals.approve(conn, ids[0], actor="system:discovery", auto=True)


# ------------------------------------------------- re-synthesis on drift (FR-0.6)
REVISED_PERSONAS = [
    {
        "id": "small_venue",
        "description": "Independent cafes and breweries that host live music.",
        "evidence_signals": ["has_events_calendar"],
        "value_prop": "free curated local acts",
        "sequence": {"steps": 2, "gaps_days": [4]},
        "cohorts": [
            {"id": "austin_retargeted", "filters": {"metro": "austin"},
             "monthly_budget": 100, "sources": ["google_places"]}
        ],
        "variants": [
            {"id": "opener_v2", "surface": "opener",
             "attributes": {"tone": "warm"},
             "prompt": "Write to {{prospect.first_name}} about "
                       "{{evidence.calendar_highlight}}. {{persona.voice_rules}}"}
        ],
    }
]


class _RevisionLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.last_prompt = ""

    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        assert task == "synthesize_program"
        self.calls += 1
        self.last_prompt = prompt
        return schema.model_validate({"personas": REVISED_PERSONAS})


def test_no_drift_means_no_revision_and_no_model_spend(
    conn: Connection, seed: Seed
) -> None:
    _add_cohort_prospects(conn, seed, "healthy", contacted=40, converted=8)
    llm = _RevisionLLM()
    assert discovery.detect_drift(conn, seed.tenant) == []
    assert discovery.resynthesize_on_drift(conn, llm, _config()) is None
    assert llm.calls == 0


def test_drift_emits_program_revision_proposal(conn: Connection, seed: Seed) -> None:
    _add_cohort_prospects(conn, seed, "dead_cohort", contacted=45, converted=0)
    llm = _RevisionLLM()
    pid = discovery.resynthesize_on_drift(conn, llm, _config())
    assert pid is not None
    assert "dead_cohort" in llm.last_prompt  # drift evidence reached the model
    prop = next(p for p in proposals.list_open(conn, seed.tenant) if p.id == pid)
    assert prop.kind == "program_revision"
    assert prop.payload["personas"][0]["cohorts"][0]["id"] == "austin_retargeted"
    assert prop.evidence["signals"]
    # always-human: a revision is a value-prop-level change (FR-0.3)
    with pytest.raises(PermissionError, match="always-human"):
        proposals.approve(conn, pid, actor="system:discovery", auto=True)
    # identical revision is deduped while the first is open
    assert discovery.resynthesize_on_drift(conn, llm, _config()) is None


# ------------------------------------------- campaign-tier research (FR-2.11)
def test_campaign_note_aggregates_market_view(conn: Connection, seed: Seed) -> None:
    from open_reachout.core import research

    _add_cohort_prospects(conn, seed, "winners", contacted=20, converted=4)
    _add_cohort_prospects(conn, seed, "losers", contacted=20, converted=0)
    note = research.refresh_campaign_note(
        conn, seed.tenant, research_directive="study venue booking dynamics"
    )
    assert note.level == "campaign" and note.subject_id == seed.tenant
    assert "Strongest cohort so far: winners" in note.summary
    assert note.findings["cohorts"]["losers"]["converted"] == 0
    assert note.findings["research_directive"].startswith("study venue")
    stored = research.latest(conn, seed.tenant, "campaign", seed.tenant)
    assert stored is not None and stored.summary == note.summary


def test_refresh_all_produces_campaign_tier_first(conn: Connection, seed: Seed) -> None:
    from open_reachout.core import research

    _add_cohort_prospects(conn, seed, "only_cohort", contacted=5, converted=1)
    n = research.refresh_all(conn, seed.tenant, research_directive="d" * 25)
    assert n >= 2  # campaign tier + per-cohort notes
    assert research.latest(conn, seed.tenant, "campaign", seed.tenant) is not None


def test_revision_carries_market_note_in_envelope(conn: Connection, seed: Seed) -> None:
    from open_reachout.core import research

    _add_cohort_prospects(conn, seed, "dead_cohort", contacted=45, converted=0)
    research.refresh_campaign_note(conn, seed.tenant, research_directive="d" * 25)
    llm = _RevisionLLM()
    pid = discovery.resynthesize_on_drift(conn, llm, _config())
    assert pid is not None
    assert "<untrusted" in llm.last_prompt          # note travels in the envelope
    assert "Market view across" in llm.last_prompt  # and carries the market summary


# ----------------------------------------------------------- rebalancing (FR-6.5)
def _floored_config(floor: float = 0.10):  # noqa: ANN202
    raw = _config().model_dump()
    raw["personas"][0]["cohorts"][0]["floors"] = {
        "conversion_rate": floor, "min_trials": 25,
    }
    from open_reachout.core.config import TenantConfig

    return TenantConfig.model_validate(raw)


def test_rebalance_flags_only_with_statistical_support(
    conn: Connection, seed: Seed
) -> None:
    cfg = _floored_config()
    cohort = cfg.personas[0].cohorts[0].id
    # below min_trials: never flagged, however bad the rate
    _add_cohort_prospects(conn, seed, cohort, contacted=10, converted=0)
    assert discovery.rebalance_scan(conn, cfg) == []
    # enough trials, rate confidently below the 10% floor
    _add_cohort_prospects(conn, seed, cohort, contacted=50, converted=0)
    ids = discovery.rebalance_scan(conn, cfg)
    assert len(ids) == 1
    prop = next(p for p in proposals.list_open(conn, seed.tenant) if p.id == ids[0])
    assert prop.kind == "rebalance"
    assert prop.payload["action"] == "pause"  # no stronger cohort to shift toward
    assert "upper bound" in prop.summary
    # dedupe: scanning again while the proposal is open files nothing new
    assert discovery.rebalance_scan(conn, cfg) == []


def test_healthy_cohort_above_floor_not_flagged(conn: Connection, seed: Seed) -> None:
    cfg = _floored_config(floor=0.05)
    cohort = cfg.personas[0].cohorts[0].id
    _add_cohort_prospects(conn, seed, cohort, contacted=60, converted=9)  # 15%
    assert discovery.rebalance_scan(conn, cfg) == []


def test_rebalance_shifts_toward_winner_and_auto_applies(
    conn: Connection, seed: Seed
) -> None:
    from datetime import UTC, datetime

    cfg = _floored_config()
    weak = cfg.personas[0].cohorts[0].id
    _add_cohort_prospects(conn, seed, weak, contacted=60, converted=0)
    _add_cohort_prospects(conn, seed, "strong_cohort", contacted=40, converted=8)
    (pid,) = discovery.rebalance_scan(conn, cfg)
    prop = next(p for p in proposals.list_open(conn, seed.tenant) if p.id == pid)
    assert prop.payload["action"] == "shift"
    assert prop.payload["to_cohort"] == "strong_cohort"
    # rebalance sits inside the hands_off envelope: auto-apply is permitted
    assert proposals.approve(conn, pid, actor="system:discovery", auto=True)
    period = datetime.now(UTC).strftime("%Y-%m")
    caps = dict(conn.execute(
        text("""SELECT scope_id, cap FROM counters
                WHERE scope_type='cohort_month' AND period=:p
                  AND scope_id IN (:a, :b)"""),
        {"p": period, "a": weak, "b": "strong_cohort"},
    ).fetchall())
    assert caps[weak] < 100 and caps["strong_cohort"] > 100


# ----------------------------------------- auto_launch_within_budget (FR-6.2)
def test_auto_review_applies_envelope_kinds_only(conn: Connection, seed: Seed) -> None:
    _add_cohort_prospects(conn, seed, "winners", contacted=40, converted=8)
    _add_cohort_prospects(conn, seed, "losers", contacted=40, converted=1)
    discovery.analyze(conn, seed.tenant)  # files a budget_shift
    discovery.brainstorm(conn, _StubLLM(), _config())  # files always-human kinds

    # propose-mode tenants: the gate does nothing
    assert proposals.auto_review(conn, seed.tenant, "propose") == []

    applied = proposals.auto_review(conn, seed.tenant, "auto_within_budget")
    assert len(applied) == 1  # the budget_shift; brainstorms stay human
    remaining = {p.kind for p in proposals.list_open(conn, seed.tenant)}
    assert "goal_brainstorm" in remaining
    assert "budget_shift" not in remaining
    actor = conn.execute(
        text("SELECT resolved_by FROM proposals WHERE id = CAST(:i AS uuid)"),
        {"i": applied[0]},
    ).scalar()
    assert actor == "system:autonomy"


# ------------------------------------------------- win/loss synthesis (FR-5.5)
def _thread(conn: Connection, seed: Seed, state: str, body: str) -> None:
    eid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute(
        text("INSERT INTO entities (id, tenant_id) VALUES (CAST(:e AS uuid), "
             "CAST(:t AS uuid))"),
        {"e": eid, "t": seed.tenant_id},
    )
    conn.execute(
        text("""INSERT INTO prospects (id, tenant_id, entity_id, cohort_id, persona_id,
                state, source_adapter, data_basis)
                VALUES (CAST(:p AS uuid), CAST(:t AS uuid), CAST(:e AS uuid), 'c', 'x',
                :s, 'fake', 'government_public')"""),
        {"p": pid, "t": seed.tenant_id, "e": eid, "s": state},
    )
    conn.execute(
        text("INSERT INTO replies (prospect_id, body) VALUES (CAST(:p AS uuid), :b)"),
        {"p": pid, "b": body},
    )


class _WinLossLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.last_prompt = ""

    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        assert task == "winloss_synth"
        self.calls += 1
        self.last_prompt = prompt
        return schema.model_validate({
            "summary": "We win on free venue accounts; we lose on band pricing.",
            "why_we_win": ["zero cost for venues"],
            "why_we_lose": ["bands balk at $9/mo"],
        })


def test_winloss_memo_needs_both_sides(conn: Connection, seed: Seed) -> None:
    from open_reachout.core import research

    llm = _WinLossLLM()
    _thread(conn, seed, "converted", "Signed up, the free account sold me.")
    assert research.winloss_memo(conn, llm, seed.tenant) is None  # no losses yet
    assert llm.calls == 0


def test_winloss_memo_synthesizes_and_reaches_digest(
    conn: Connection, seed: Seed
) -> None:
    from open_reachout.core import research
    from open_reachout.core.report import build_report

    _thread(conn, seed, "converted", "Signed up, the free account sold me.")
    _thread(conn, seed, "declined", "Nine bucks a month is too rich for us.")
    llm = _WinLossLLM()
    note = research.winloss_memo(conn, llm, seed.tenant)
    assert note is not None and note.level == "winloss"
    assert "<untrusted" in llm.last_prompt           # threads are enveloped
    digest = build_report(conn)
    assert "zero cost for venues" in digest
    assert "bands balk at $9/mo" in digest
