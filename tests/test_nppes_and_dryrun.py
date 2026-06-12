from pathlib import Path

import pytest
from typer.testing import CliRunner

from open_reachout.adapters.sources.nppes import NppesSource
from open_reachout.cli.main import app

FIXTURE = Path(__file__).parent / "fixtures" / "nppes_sample.csv"
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_nppes_filters_state_taxonomy_and_individuals() -> None:
    source = NppesSource(FIXTURE)
    result = source.discover({"state": "TX", "taxonomy": ["106H00000X"]}, None)
    names = [c.display_name for c in result.candidates]
    # Orgs (entity 2), other states, other taxonomies, and nameless rows drop out.
    assert names == ["Elena Garcia", "Marcus Chen"]
    first = result.candidates[0]
    assert first.source_ref["npi"] == "1000000001"
    assert first.email_raw is None  # NPPES has no emails — enrichment's job
    assert first.data_basis == "government_public"
    assert "AUSTIN" in (first.address or "")


def test_nppes_requires_filters() -> None:
    with pytest.raises(ValueError, match="state"):
        NppesSource(FIXTURE).discover({}, None)


@pytest.mark.gates
def test_gate14_examples_dry_run_end_to_end(tmp_path: Path) -> None:
    """Gate 14: both reference configs complete a dry-run cycle."""
    runner = CliRunner()
    for tenant_dir in ("music-marketplace", "therapist-directory"):
        out = tmp_path / f"{tenant_dir}.md"
        result = runner.invoke(
            app,
            ["dry-run", str(EXAMPLES / tenant_dir / "tenant.yaml"), "--n", "2",
             "--out", str(out)],
        )
        assert result.exit_code == 0, result.output
        review = out.read_text()
        assert "would-send drafts" in review
        assert "Nothing was sent" in review
        # CAN-SPAM completeness survives the whole pipeline (gate 9 end-to-end).
        assert "drafts, 0 disqualified" in result.output or "## " in review


def test_dry_run_with_nppes_source(tmp_path: Path) -> None:
    out = tmp_path / "review.md"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["dry-run", str(EXAMPLES / "therapist-directory" / "tenant.yaml"),
         "--n", "2", "--out", str(out), "--nppes-csv", str(FIXTURE)],
    )
    assert result.exit_code == 0, result.output
    # TX LMFT cohort matches fixture rows; GA LPC cohort matches the GA row.
    assert "Elena Garcia" in out.read_text()
