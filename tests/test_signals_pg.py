"""Signal-kind sources (FR-2.2): license filings become deduped timing events
that fire trigger cohorts with selector-narrowed discovery.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from tests.conftest import Seed

from open_reachout.adapters.sources.signals import ingest_license_csv
from open_reachout.core import prospecting
from open_reachout.core.config import TenantConfig, load_tenant
from open_reachout.core.worker import Worker

pytestmark = pytest.mark.postgres

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

CSV = """Licensee,City,St,Issue Date,Type
Cactus Cantina,Austin,TX,2026-06-01,Mixed Beverage
Hilltop Hall,Austin,TX,2026-06-03,Beer & Wine
,Austin,TX,2026-06-04,Beer & Wine
"""


def _write_csv(tmp_path: Path) -> Path:
    p = tmp_path / "abc_filings.csv"
    p.write_text(CSV)
    return p


def test_csv_rows_become_deduped_events(
    conn: Connection, seed: Seed, tmp_path: Path
) -> None:
    path = _write_csv(tmp_path)
    rows, fired = ingest_license_csv(conn, path)
    assert rows == 3 and fired == 2  # blank licensee dropped
    selectors = [
        r[0] for r in conn.execute(
            text("SELECT selector FROM operator_events WHERE event_type = 'license.issued'")
        )
    ]
    assert {s["business_name"] for s in selectors} == {"Cactus Cantina", "Hilltop Hall"}
    # re-ingest: pure no-op (jurisdiction+licensee+date dedupe)
    rows, fired = ingest_license_csv(conn, path)
    assert rows == 3 and fired == 0
    trigger_jobs = conn.execute(
        text("SELECT count(*) FROM jobs WHERE queue = 'trigger'")
    ).scalar()
    assert trigger_jobs == 2


def test_signal_fires_triggered_cohort_with_narrowed_discovery(
    pg_engine: Engine, conn: Connection, seed: Seed, tmp_path: Path
) -> None:
    raw = load_tenant(EXAMPLES / "music-marketplace" / "tenant.yaml").model_dump()
    cohorts = raw["personas"][0]["cohorts"]
    cohorts[0]["monthly_budget"] = 150
    cohorts.append({
        "id": "new_licensees", "filters": {"metro": "austin"},
        "monthly_budget": 50, "sources": ["google_places"],
        "trigger": {"event_type": "license.issued"},
    })
    cfg = TenantConfig.model_validate(raw)
    runtime = prospecting.runtime_for(conn, cfg)
    ingest_license_csv(conn, _write_csv(tmp_path))
    conn.commit()
    Worker(pg_engine, handlers={
        "trigger": prospecting.make_trigger_handler({cfg.tenant: runtime}),
    }).drain()
    with pg_engine.begin() as c2:
        discovers = c2.execute(
            text(
                """SELECT payload FROM jobs WHERE queue = 'discover'
                   AND payload->>'cohort' = 'new_licensees'"""
            )
        ).fetchall()
        names = {r[0]["extra_filters"]["business_name"] for r in discovers}
        assert names == {"Cactus Cantina", "Hilltop Hall"}
