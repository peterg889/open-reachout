"""Demo seeder: a realistic populated deployment for presenting the dashboard.

Runs the REAL pipeline (discover -> entity resolution -> enrich -> qualify ->
compose -> gated claim -> dispatch) with fake edges, then simulates a spread
of replies (interested/question/declined/unsubscribe) and conversions, runs
the discovery agent, and refreshes research notes — so every dashboard view
has honest content produced by the actual machinery, not hand-inserted rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Engine

from open_reachout.adapters.fakes import (
    FakeEnricher,
    FakeFinder,
    FakeSendingProvider,
    FakeSource,
    FakeVerifier,
)
from open_reachout.agents import discovery
from open_reachout.core import attribution, dryrun, events, prospecting, research, sendpath
from open_reachout.core.config import load_tenant
from open_reachout.core.interfaces import Candidate, DataBasis
from open_reachout.core.worker import Worker

VENUES = [
    ("Cactus Cafe", "thursday open-mic and weekend songwriter series"),
    ("Hops & Vine Brewery", "saturday patio shows, no booker on staff"),
    ("Red River Listening Room", "curated americana calendar, books 8 weeks out"),
    ("Eastside Wine Garden", "sunday jazz brunch, looking for duos"),
    ("Mole Hill Tavern", "open booking, mostly walk-in bands"),
    ("Lantern Coffee", "acoustic evenings twice a month"),
]
BANDS = [
    ("The Prickly Pears", "indie folk trio, 40 local shows last year"),
    ("Marrow & Pine", "americana duo, weekly residency experience"),
    ("Glass Canyon", "surf rock four-piece, draws ~60"),
    ("June Tides", "jazz quartet, brunch and winery sets"),
]
REPLY_SCRIPT: list[tuple[str, str, str]] = [
    # (member display name, reply body, classifier intent)
    ("Cactus Cafe", "This sounds genuinely useful - how does booking work?", "question"),
    ("Hops & Vine Brewery", "Yes! We've needed exactly this. Send the link.", "interested"),
    ("Red River Listening Room", "We book through an agent already, thanks.", "not_interested"),
    ("Eastside Wine Garden", "please remove me from your list", "unsubscribe"),
    ("The Prickly Pears", "Interested - we're trying to fill March.", "interested"),
    ("Marrow & Pine", "who gave you this address??", "hostile"),
]
CONVERTERS = ("Hops & Vine Brewery", "The Prickly Pears")


class _ScriptedClassifier:
    """Classifies demo replies per the script (the live path uses Gemini)."""

    def __init__(self, script: dict[str, str]) -> None:
        self.script = script

    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        intent = next((i for frag, i in self.script.items() if frag in prompt), "other")
        return schema.model_validate({"intent": intent, "confidence": 0.95})


def seed_demo(engine: Engine, config_path: Path) -> dict[str, int]:
    cfg = load_tenant(config_path)
    with engine.begin() as conn:
        runtime = prospecting.runtime_for(conn, cfg)
        conn.execute(
            text(
                """INSERT INTO mailboxes (mailbox, tenant, domain, warmup_complete)
                   VALUES ('maya@get-stagematch.com', :t, 'get-stagematch.com', true)
                   ON CONFLICT (mailbox) DO NOTHING"""
            ),
            {"t": cfg.tenant},
        )
        period = datetime.now(UTC).strftime("%Y-%m")
        for p in cfg.personas:
            for c in p.cohorts:
                conn.execute(
                    text(
                        """INSERT INTO counters (scope_type, scope_id, period, cap)
                           VALUES ('cohort_month', :c, :p, 200)
                           ON CONFLICT DO NOTHING"""
                    ),
                    {"c": c.id, "p": period},
                )
        conn.execute(
            text(
                """INSERT INTO counters (scope_type, scope_id, period, cap)
                   VALUES ('tenant_month', :t, :p, 500) ON CONFLICT DO NOTHING"""
            ),
            {"t": cfg.tenant, "p": period},
        )
        prospecting.seed_discovery(conn, runtime)

    def candidates(pairs: list[tuple[str, str]]) -> list[Candidate]:
        return [
            Candidate(
                display_name=name, org_name=name, website="https://venue.example",
                email_raw=f"{name.lower().replace(' ', '.').replace('&', 'and')}@example.test",
                source_adapter="demo", source_ref={"place_id": name},
                data_basis=DataBasis.GOVERNMENT_PUBLIC,
            )
            for name, _bio in pairs
        ]

    class DemoEnricher(FakeEnricher):
        bios = dict(VENUES) | dict(BANDS)

        def enrich(self, candidate: Candidate):  # type: ignore[override]
            card = super().enrich(candidate)
            bio = self.bios.get(candidate.display_name or "", "")
            if bio:
                card.facts[0] = card.facts[0].model_copy(
                    update={"content": f"{candidate.display_name}: {bio}",
                            "fact_type": "calendar"}
                )
            return card

    venue_sources = {"google_places": FakeSource(candidates(VENUES)),
                     "indie_on_the_move": FakeSource([])}
    band_sources = {"bandsintown": FakeSource(candidates(BANDS)),
                    "bandcamp": FakeSource([])}
    sources = venue_sources | band_sources

    with engine.begin() as conn:
        runtime = prospecting.runtime_for(conn, cfg)
    runtimes = {cfg.tenant: runtime}
    scripted = dryrun.ScriptedLLM(runtime.validator_ctx, cfg.brief.about_us.identity.sender)
    classifier = _ScriptedClassifier({frag: intent for frag, _, intent in
                                      [(b, b, i) for _, b, i in REPLY_SCRIPT]})
    provider = FakeSendingProvider()
    worker = Worker(
        engine,
        handlers={
            "discover": prospecting.make_discover_handler(runtimes, sources),
            "enrich": prospecting.make_enrich_handler(
                runtimes, DemoEnricher(), FakeFinder(), FakeVerifier()
            ),
            "qualify": prospecting.make_qualify_handler(runtimes, scripted),
            "compose": prospecting.make_compose_handler(runtimes, scripted),
            "deliver": sendpath.make_deliver_handler(
                provider, {cfg.tenant: runtime.validator_ctx}
            ),
            "classify": events.make_classify_handler(classifier),
            "control": events.make_control_handler(provider),
        },
    )
    worker.drain()

    # Simulate inbound replies via the webhook path (signature-verified).
    with engine.begin() as conn:
        touch_by_name = dict(
            conn.execute(
                text(
                    """
                    SELECT e.display_name, tc.id FROM touches tc
                    JOIN prospects p ON p.id = tc.prospect_id
                    JOIN entities e ON e.id = p.entity_id
                    WHERE tc.status = 'dispatched'
                    """
                )
            ).fetchall()
        )
        import json as _json

        for i, (name, body, _intent) in enumerate(REPLY_SCRIPT):
            touch_id = touch_by_name.get(name)
            if touch_id is None:
                continue
            raw = _json.dumps(
                {"id": f"demo-reply-{i}", "kind": "reply",
                 "touch_ref": {"touch_id": str(touch_id)}, "payload": {"body": body}}
            ).encode()
            events.ingest_webhook(conn, provider, raw, provider.sign(raw))
    worker.drain()

    # Conversions via the attribution path; then discovery + research.
    converted = 0
    with engine.begin() as conn:
        for name in CONVERTERS:
            touch_id = touch_by_name.get(name)
            if touch_id is not None and attribution.record_conversion(conn, str(touch_id)):
                converted += 1
        discovery.analyze(conn, cfg.tenant)
        notes = research.refresh_all(conn, cfg.tenant)
        sent = conn.execute(
            text("SELECT count(*) FROM touches WHERE status IN ('dispatched','sent')")
        ).scalar()
        replies = conn.execute(text("SELECT count(*) FROM replies")).scalar()
    return {"sent": int(sent or 0), "replies": int(replies or 0),
            "converted": converted, "research_notes": notes}
