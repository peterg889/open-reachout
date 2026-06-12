"""Program synthesis (PRD FR-0.2): schemas are the enforcement layer."""

from pathlib import Path

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from open_reachout.agents import synthesizer
from open_reachout.cli.main import app
from open_reachout.core.config import dump_tenant, load_brief, load_tenant

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
BRIEF = load_brief(EXAMPLES / "music-marketplace" / "tenant.yaml")


def good_personas() -> list[dict]:
    return [
        {
            "id": "small_venue",
            "description": "Independent cafes and breweries that host live music.",
            "evidence_signals": ["has_events_calendar"],
            "value_prop": "free curated local acts",
            "sequence": {"steps": 2, "gaps_days": [4]},
            "cohorts": [
                {"id": "austin", "filters": {}, "monthly_budget": 100,
                 "sources": ["google_places"]}
            ],
            "variants": [
                {"id": "opener_v1", "surface": "opener",
                 "attributes": {"tone": "warm"},
                 "prompt": "Write to {{prospect.first_name}} about "
                           "{{evidence.calendar_highlight}}. {{persona.voice_rules}}"}
            ],
        }
    ]


class SeqLLM:
    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = list(outputs)
        self.calls = 0

    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        self.calls += 1
        return schema.model_validate(self.outputs.pop(0))


@pytest.mark.gates
def test_template_program_validates_and_roundtrips(tmp_path: Path) -> None:
    """FR-0.2/0.4: synthesized artifacts are ordinary config — one system."""
    cfg = synthesizer.template_program(BRIEF, "newco")
    assert cfg.generated_by and cfg.generated_by.agent.startswith("synthesizer@")
    path = tmp_path / "tenant.yaml"
    path.write_text(dump_tenant(cfg))
    reloaded = load_tenant(path)  # full validation on the way back in
    assert reloaded.tenant == "newco" and len(reloaded.personas) == 1
    total = sum(c.monthly_budget for p in reloaded.personas for c in p.cohorts)
    assert total <= BRIEF.budgets.monthly_prospects


def test_synthesis_retries_with_validation_feedback_then_succeeds() -> None:
    bad = good_personas()
    bad[0]["variants"][0]["prompt"] = "Use {{secret.api_key}} in the email somehow ok"
    llm = SeqLLM([{"personas": bad}, {"personas": good_personas()}])
    cfg = synthesizer.synthesize(llm, BRIEF, "newco")
    assert llm.calls == 2
    assert cfg.personas[0].variants[0].id == "opener_v1"


def test_synthesis_cannot_exceed_budget_and_escalates() -> None:
    """The synthesizer cannot raise caps: TenantConfig enforces, prompt or not."""
    over = good_personas()
    over[0]["cohorts"][0]["monthly_budget"] = BRIEF.budgets.monthly_prospects + 1
    llm = SeqLLM([{"personas": over}] * 3)
    with pytest.raises(synthesizer.SynthesisEscalation, match="cohort budgets"):
        synthesizer.synthesize(llm, BRIEF, "newco")


def test_followup_cap_unraisable_by_synthesis() -> None:
    five_step = good_personas()
    five_step[0]["sequence"] = {"steps": 5, "gaps_days": [4, 4, 4, 4]}
    llm = SeqLLM([{"personas": five_step}] * 3)
    with pytest.raises(synthesizer.SynthesisEscalation):
        synthesizer.synthesize(llm, BRIEF, "newco")


def test_cli_init_writes_program_and_proposal(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["init", "--from-brief", str(EXAMPLES / "music-marketplace" / "tenant.yaml"),
         "--tenant", "newco", "--out", str(tmp_path), "--sample", "2"],
    )
    assert result.exit_code == 0, result.output
    cfg = load_tenant(tmp_path / "tenant.yaml")
    assert cfg.generated_by is not None
    proposal = (tmp_path / "program-proposal.md").read_text()
    assert "# Program Proposal" in proposal
    assert "## Cohorts" in proposal
    assert "Nothing was sent" in proposal
    # validate the generated file through the CLI too (one config system)
    check = CliRunner().invoke(app, ["validate", str(tmp_path / "tenant.yaml")])
    assert check.exit_code == 0, check.output


def test_gemini_schema_adapter_inlines_refs_and_drops_additional_props() -> None:
    """The Gemini Developer API rejects additionalProperties and $ref;
    strictness stays local (pydantic model_validate on the way back in)."""
    from open_reachout.adapters.llm.gemini_backend import _gemini_schema
    from open_reachout.agents.schemas import ComposeOutput

    schema = _gemini_schema(ComposeOutput)
    flat = repr(schema)
    assert "additionalProperties" not in flat
    assert "$ref" not in flat and "$defs" not in flat
    # nested Claim model survived inlining
    assert schema["properties"]["claims"]["items"]["properties"]["fact_id"]


def test_gemini_schema_softens_exclusive_bounds() -> None:
    from open_reachout.adapters.llm.gemini_backend import _gemini_schema
    from open_reachout.agents.synthesizer import SynthesizedProgram

    schema = repr(_gemini_schema(SynthesizedProgram))
    assert "exclusiveMinimum" not in schema and "exclusiveMaximum" not in schema
    assert "'minimum'" in schema  # gt=0 fields keep a usable bound
