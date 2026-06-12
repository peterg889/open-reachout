from pathlib import Path

import pytest

from open_reachout import MAX_FOLLOW_UPS
from open_reachout.core.config import ConfigError, expand_autonomy, load_tenant
from open_reachout.core.states import ProspectState, TransitionError, assert_transition

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.parametrize("tenant_dir", ["music-marketplace", "therapist-directory"])
def test_examples_validate(tenant_dir: str) -> None:
    cfg = load_tenant(EXAMPLES / tenant_dir / "tenant.yaml")
    assert cfg.personas
    total = sum(c.monthly_budget for p in cfg.personas for c in p.cohorts)
    assert total <= cfg.brief.budgets.monthly_prospects


def test_followup_cap_is_unraisable(tmp_path: Path) -> None:
    """PRD FR-3.5: the 3-follow-up cap is a core constant, not config."""
    raw = (EXAMPLES / "music-marketplace" / "tenant.yaml").read_text()
    raw = raw.replace(
        "steps: 3\n      gaps_days: [4, 7]", "steps: 5\n      gaps_days: [4, 7, 4, 4]"
    )
    bad = tmp_path / "tenant.yaml"
    bad.write_text(raw)
    with pytest.raises(ConfigError, match="less than or equal to"):
        load_tenant(bad)
    assert MAX_FOLLOW_UPS == 3


def test_unknown_prompt_slot_rejected(tmp_path: Path) -> None:
    raw = (EXAMPLES / "music-marketplace" / "tenant.yaml").read_text()
    raw = raw.replace("{{persona.value_prop}}", "{{secret.api_key}}")
    bad = tmp_path / "tenant.yaml"
    bad.write_text(raw)
    with pytest.raises(ConfigError, match="unknown variable slot"):
        load_tenant(bad)


def test_budget_overcommit_rejected(tmp_path: Path) -> None:
    raw = (EXAMPLES / "music-marketplace" / "tenant.yaml").read_text()
    raw = raw.replace("monthly_budget: 300", "monthly_budget: 9000")
    bad = tmp_path / "tenant.yaml"
    bad.write_text(raw)
    with pytest.raises(ConfigError, match="cohort budgets"):
        load_tenant(bad)


def test_autonomy_presets_expand() -> None:
    assert expand_autonomy("hands_off").cohort_launch == "auto_within_budget"
    assert expand_autonomy("review_everything").reply_actions == "propose"
    assert expand_autonomy("standard").cohort_launch == "propose"


# ----------------------------------------------------------------- states
def test_happy_lifecycle_path() -> None:
    path = [
        ProspectState.DISCOVERED,
        ProspectState.ENRICHED,
        ProspectState.QUALIFIED,
        ProspectState.QUEUED,
        ProspectState.CONTACTED,
        ProspectState.ENGAGED,
        ProspectState.CONVERTED,
    ]
    for current, target in zip(path, path[1:], strict=False):
        assert_transition(current, target)


def test_forgotten_is_reachable_from_everywhere_and_terminal() -> None:
    for state in ProspectState:
        if state is ProspectState.FORGOTTEN:
            continue
        assert_transition(state, ProspectState.FORGOTTEN)
    with pytest.raises(TransitionError):
        assert_transition(ProspectState.FORGOTTEN, ProspectState.DISCOVERED)


def test_illegal_shortcuts_rejected() -> None:
    with pytest.raises(TransitionError):
        assert_transition(ProspectState.DISCOVERED, ProspectState.CONTACTED)
    with pytest.raises(TransitionError):
        assert_transition(ProspectState.CONVERTED, ProspectState.QUEUED)


def test_apply_schema_is_idempotent(conn) -> None:  # noqa: ANN001
    """Upgrades re-apply schema.sql on live databases: running it twice must
    be safe and must not disturb existing rows (spec §17 expand-contract)."""
    import pytest

    pytest.importorskip("psycopg")
    from sqlalchemy import text

    from open_reachout.core.db import apply_schema

    conn.execute(text("INSERT INTO tenants (slug) VALUES ('idem-check')"))
    apply_schema(conn)  # second application over a populated database
    survived = conn.execute(
        text("SELECT count(*) FROM tenants WHERE slug = 'idem-check'")
    ).scalar()
    assert survived == 1
