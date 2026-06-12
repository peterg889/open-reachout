"""Dashboard + demo seeder: the presentable UI over real pipeline data."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from tests.conftest import _TABLES

from open_reachout.adapters.fakes import FakeSendingProvider
from open_reachout.api.app import ApiToken, create_app
from open_reachout.core import metrics, research
from open_reachout.core.demo import seed_demo

pytestmark = pytest.mark.postgres

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
TENANT_YAML = EXAMPLES / "music-marketplace" / "tenant.yaml"


@pytest.fixture
def demo(pg_engine: Engine) -> dict[str, int]:
    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(_TABLES) + " RESTART IDENTITY CASCADE"))
    return seed_demo(pg_engine, TENANT_YAML)


@pytest.fixture
def client(pg_engine: Engine) -> TestClient:
    app = create_app(pg_engine, FakeSendingProvider(), attribution_key=b"k" * 16,
                     tokens=[ApiToken("t", "s" * 24, frozenset({"read"}))])
    return TestClient(app)


def test_demo_seeds_a_full_world(pg_engine: Engine, demo: dict[str, int]) -> None:
    assert demo["sent"] >= 8  # 6 venues + 4 bands, minus any drops
    assert demo["replies"] == 6
    assert demo["converted"] == 2
    assert demo["research_notes"] >= 3  # 2 cohorts + strategies

    with pg_engine.begin() as conn:
        f = metrics.funnel(conn, "stagematch")
        assert f.contacted >= 8
        assert f.positive_replies == 2  # interested intents
        assert f.converted == 2
        assert "after contact (opted out)" in f.exits  # the unsubscribe
        note = research.latest(conn, "stagematch", "cohort", "austin_venues_2026q3")
        assert note is not None and "contacted" in note.summary


def test_overview_shows_metrics_funnel_and_cohorts(
    demo: dict[str, int], client: TestClient
) -> None:
    page = client.get("/dashboard").text
    assert "reached (contacted)" in page
    assert "Abandonment" in page and "opted out" in page
    assert "austin_venues_2026q3" in page and "austin_bands_2026q3" in page
    assert "converted" in page


def test_cohort_page_shows_strategies_members_and_research(
    demo: dict[str, int], client: TestClient
) -> None:
    page = client.get(
        "/dashboard/cohort/austin_venues_2026q3", params={"tenant": "stagematch"}
    ).text
    assert "Cohort research" in page and "contacted" in page
    assert "Strategies being tested" in page and "opener_calendar_hook" in page
    assert "Cactus Cafe" in page and "Hops &amp; Vine Brewery" in page


def test_member_page_shows_evidence_and_conversation(
    pg_engine: Engine, demo: dict[str, int], client: TestClient
) -> None:
    with pg_engine.begin() as conn:
        rows = metrics.members(conn, "stagematch", "austin_venues_2026q3")
    cactus = next(m for m in rows if "Cactus" in m.display_name)
    page = client.get(
        f"/dashboard/member/{cactus.prospect_id}", params={"tenant": "stagematch"}
    ).text
    # Background research with provenance...
    assert "Background research" in page
    assert "open-mic" in page and "observed" in page
    # ...and the full conversation: our send plus their reply.
    assert "Conversation history" in page
    assert "sent" in page and "reply" in page
    assert "how does booking work" in page


def test_dashboard_token_gate(demo: dict[str, int], client: TestClient,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OR_DASHBOARD_TOKEN", "secret-token")
    assert client.get("/dashboard").status_code == 401
    assert client.get("/dashboard", params={"token": "secret-token"}).status_code == 200


def test_campaign_page_shows_research_and_cohorts(
    demo: dict[str, int], client: TestClient
) -> None:
    page = client.get("/dashboard/campaign/stagematch").text
    assert "Campaign research" in page or "No campaign-tier research" in page
    assert "Cohorts in this campaign" in page
    assert "austin_venues_2026q3" in page


def test_member_state_ledger_explains_transitions(
    demo: dict[str, int], client: TestClient
) -> None:
    import re

    overview = client.get(
        "/dashboard/cohort/austin_venues_2026q3?tenant=stagematch"
    ).text
    member = re.search(r"/dashboard/member/[a-f0-9-]+", overview).group(0)
    page = client.get(f"{member}?tenant=stagematch").text
    assert "State ledger" in page
    assert "reason" in page  # the why column exists with transitions in it


def test_friendly_names_render_with_slug_secondary(
    demo: dict[str, int], client: TestClient
) -> None:
    page = client.get("/dashboard").text
    assert "Austin Venues — 2026 Q3" in page  # friendly
    assert "austin_venues_2026q3" in page     # raw id stays discoverable


def test_campaign_outbox_shows_ready_emails_and_halt_banner(
    demo: dict[str, int], client: TestClient, pg_engine: Engine
) -> None:
    from open_reachout.core import control, sendpath
    from open_reachout.core.compliance.validators import Draft, content_hash

    with pg_engine.begin() as conn:
        pid = conn.execute(text("""SELECT p.id FROM prospects p
            JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = 'stagematch' LIMIT 1""")).scalar()
        draft = Draft(subject="A Thursday idea", body="b")
        sendpath.queue_draft(
            conn, prospect_id=str(pid), campaign_id="c", variant_id="v",
            step_index=0, kind="cold", draft=draft,
            content_hash=content_hash(draft),
        )
        control.halt(conn, scope="stagematch", actor="operator:test")
    page = client.get("/dashboard/campaign/stagematch").text
    assert "Outbox" in page
    assert "ready to send" in page
    assert "A Thursday idea" in page
    assert "Sending is HALTED" in page          # the why-it-isn't-going banner
    overview = client.get("/dashboard").text
    assert "ready to send" in overview
