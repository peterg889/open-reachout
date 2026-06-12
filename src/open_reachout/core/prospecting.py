"""Prospecting worker handlers (spec 8.1-8.4): the live discover -> enrich ->
qualify -> compose -> queue path, wiring the M1 pipeline through the worker
with real persistence and entity resolution.

`dry_run` exercises the same agents standalone; this module is the durable,
gated version: prospects and entities land in Postgres, ingest screening drops
suppressed/forgotten/denylisted candidates, and qualified prospects produce
drafted touches the deliver handler claims and sends.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.agents.composer import ComposeEscalation, ComposeInputs, compose
from open_reachout.agents.qualifier import qualify
from open_reachout.core import dryrun, queue, suppression
from open_reachout.core.compliance.validators import ValidatorContext
from open_reachout.core.config import PersonaSpec, TenantConfig
from open_reachout.core.entity import resolve_entity
from open_reachout.core.escalations import escalate
from open_reachout.core.interfaces import (
    Candidate,
    ConfidenceBucket,
    EmailFinder,
    Enricher,
    EvidenceCard,
    LLMBackend,
    SourceAdapter,
    Verifier,
)
from open_reachout.core.lifecycle import transition
from open_reachout.core.queue import Job
from open_reachout.core.sendpath import queue_draft
from open_reachout.core.states import ProspectState
from open_reachout.core.worker import Handler, PermanentJobError
from open_reachout.stats.persistence import select_variant

# Denylisted source domains (spec 7.2): config may extend, never shrink.
SOURCE_DENYLIST = ("psychologytoday.com", "yelp.com")


@dataclass(frozen=True)
class TenantRuntime:
    """Resolved per-tenant wiring the prospecting handlers need."""

    config: TenantConfig
    tenant_id: str
    validator_ctx: ValidatorContext


def _tenant_id(conn: Connection, slug: str) -> str:
    row = conn.execute(
        text(
            """
            INSERT INTO tenants (slug) VALUES (:s)
            ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug
            RETURNING id
            """
        ),
        {"s": slug},
    ).fetchone()
    assert row is not None
    return str(row[0])


def _denylisted(candidate: Candidate) -> bool:
    target = (candidate.website or "") + " " + json.dumps(candidate.source_ref)
    return any(bad in target.lower() for bad in SOURCE_DENYLIST)


def ingest_candidate(
    conn: Connection, runtime: TenantRuntime, persona: PersonaSpec, cohort_id: str,
    candidate: Candidate,
) -> str | None:
    """Screen + resolve + persist one candidate as a `discovered` prospect.
    Returns the prospect id, or None if dropped (denylist/suppression/dupe)."""
    if _denylisted(candidate):
        return None
    if candidate.email_raw and not suppression.screen_at_ingest(
        conn, candidate.email_raw, runtime.config.tenant
    ):
        return None  # suppressed/forgotten: dropped silently (gate 5)

    resolution = resolve_entity(conn, runtime.tenant_id, candidate)
    if resolution.merge_conflict:
        escalate(
            conn, tenant=runtime.config.tenant, subject_type="entity",
            subject_id=resolution.entity_id, reason="merge conflict at ingest",
            payload={"candidate": candidate.display_name},
        )

    # One prospect per (cohort, entity): a re-discovery is a no-op.
    existing = conn.execute(
        text(
            """
            SELECT id FROM prospects WHERE cohort_id = :c AND entity_id = CAST(:e AS uuid)
            """
        ),
        {"c": cohort_id, "e": resolution.entity_id},
    ).fetchone()
    if existing is not None:
        return None

    prospect_id = str(uuid.uuid4())
    conn.execute(
        text(
            """
            INSERT INTO prospects (id, tenant_id, entity_id, cohort_id, persona_id, state,
                email_raw, source_adapter, source_ref, data_basis)
            VALUES (CAST(:i AS uuid), CAST(:t AS uuid), CAST(:e AS uuid), :c, :pe,
                'discovered', :em, :sa, CAST(:sr AS jsonb), :db)
            """
        ),
        {"i": prospect_id, "t": runtime.tenant_id, "e": resolution.entity_id, "c": cohort_id,
         "pe": persona.id, "em": candidate.email_raw, "sa": candidate.source_adapter,
         "sr": json.dumps(candidate.source_ref), "db": candidate.data_basis},
    )
    queue.enqueue(conn, "enrich", {"prospect_id": prospect_id},
                  idempotency_key=f"enrich:{prospect_id}")
    return prospect_id


# --------------------------------------------------------------------- handlers
def make_discover_handler(
    runtimes: dict[str, TenantRuntime], sources: dict[str, SourceAdapter]
) -> Handler:
    def discover(conn: Connection, job: Job) -> None:
        runtime = runtimes[str(job.payload["tenant"])]
        persona = next(p for p in runtime.config.personas if p.id == job.payload["persona"])
        cohort = next(c for c in persona.cohorts if c.id == job.payload["cohort"])
        source = next((sources[s] for s in cohort.sources if s in sources), None)
        if source is None:
            raise PermanentJobError(f"no source adapter for cohort {cohort.id}")
        result = source.discover(cohort.filters, job.payload.get("cursor"))
        for candidate in result.candidates:
            ingest_candidate(conn, runtime, persona, cohort.id, candidate)
        if result.cursor:  # page through large sources
            queue.enqueue(
                conn, "discover",
                {**job.payload, "cursor": result.cursor},
                idempotency_key=f"discover:{cohort.id}:{result.cursor}",
            )

    return discover


def make_enrich_handler(
    runtimes: dict[str, TenantRuntime], enricher: Enricher, finder: EmailFinder,
    verifier: Verifier,
) -> Handler:
    def enrich(conn: Connection, job: Job) -> None:
        prospect_id = str(job.payload["prospect_id"])
        candidate, tenant = _load_candidate(conn, prospect_id)
        if candidate is None:
            return  # forgotten/deleted between discover and enrich
        card = enricher.enrich(candidate)
        _store_evidence(conn, prospect_id, card)

        # Email: own-site/registry value first, else the finder waterfall.
        if candidate.email_raw:
            address: str | None = candidate.email_raw
        else:
            found = finder.find(candidate)
            address = found.email if found else None
        if address is None:
            transition(conn, prospect_id, ProspectState.UNENRICHABLE, actor="system:enrich")
            return
        bucket = verifier.verify(address).bucket
        conn.execute(
            text(
                """
                UPDATE prospects SET email_raw = :e, email_canonical = :c,
                    email_confidence = :b WHERE id = CAST(:i AS uuid)
                """
            ),
            {"e": address, "c": _canon(address), "b": bucket.value, "i": prospect_id},
        )
        if bucket is not ConfidenceBucket.VERIFIED:
            transition(conn, prospect_id, ProspectState.UNENRICHABLE, actor="system:enrich")
            return
        transition(conn, prospect_id, ProspectState.ENRICHED, actor="system:enrich")
        queue.enqueue(conn, "qualify", {"prospect_id": prospect_id},
                      idempotency_key=f"qualify:{prospect_id}")

    return enrich


def make_qualify_handler(runtimes: dict[str, TenantRuntime], llm: LLMBackend) -> Handler:
    def qualify_handler(conn: Connection, job: Job) -> None:
        prospect_id = str(job.payload["prospect_id"])
        runtime, persona, card = _load_for_qualify(conn, runtimes, prospect_id)
        if persona is None:
            return
        verdict = qualify(llm, card, persona.description)
        if verdict.escalate:
            escalate(conn, tenant=runtime.config.tenant, subject_type="prospect",
                     subject_id=prospect_id, reason="injection suspicion during qualification")
            transition(conn, prospect_id, ProspectState.DISQUALIFIED, actor="system:qualify")
            return
        if not verdict.qualified:
            transition(conn, prospect_id, ProspectState.DISQUALIFIED,
                       actor="system:qualify", reason=verdict.rationale[:200])
            return
        transition(conn, prospect_id, ProspectState.QUALIFIED, actor="system:qualify")
        transition(conn, prospect_id, ProspectState.QUEUED, actor="system:qualify")
        queue.enqueue(conn, "compose", {"prospect_id": prospect_id},
                      idempotency_key=f"compose:{prospect_id}")

    return qualify_handler


def make_compose_handler(runtimes: dict[str, TenantRuntime], llm: LLMBackend) -> Handler:
    def compose_handler(conn: Connection, job: Job) -> None:
        prospect_id = str(job.payload["prospect_id"])
        runtime, persona, card, candidate = _load_for_compose(conn, runtimes, prospect_id)
        if persona is None:
            return
        variant, _posterior = select_variant(conn, runtime.config.tenant, persona.variants)
        trusted = dryrun.trusted_context(runtime.config, persona)
        values = dryrun.build_values(variant.prompt, candidate, card, runtime.config, persona)
        try:
            result = compose(
                llm,
                ComposeInputs(
                    variant_id=variant.id, variant_prompt=variant.prompt, values=values,
                    validator_ctx=runtime.validator_ctx, trusted_context=trusted,
                ),
            )
        except (ComposeEscalation, KeyError) as exc:
            escalate(conn, tenant=runtime.config.tenant, subject_type="prospect",
                     subject_id=prospect_id, reason=f"compose failed: {exc}")
            return
        queue_draft(
            conn, prospect_id=prospect_id, campaign_id=f"{persona.id}:{variant.surface}",
            variant_id=variant.id, step_index=0, kind="cold",
            draft=result.draft, content_hash=result.content_sha256,
        )

    return compose_handler


# ----------------------------------------------------------------- load helpers
def _canon(address: str) -> str | None:
    from open_reachout.core.canonical import InvalidEmailError, canonicalize

    try:
        return canonicalize(address)
    except InvalidEmailError:
        return None


def _candidate_from_row(row: tuple) -> Candidate:
    name, website, email, source_adapter, source_ref, data_basis, org = row
    return Candidate(
        display_name=name or "Unknown", org_name=org, website=website, email_raw=email,
        source_adapter=source_adapter, source_ref=source_ref or {}, data_basis=data_basis,
    )


def _load_candidate(conn: Connection, prospect_id: str) -> tuple[Candidate | None, str | None]:
    row = conn.execute(
        text(
            """
            SELECT e.display_name, p.email_raw, p.source_adapter, p.source_ref,
                   p.data_basis, t.slug
            FROM prospects p JOIN entities e ON e.id = p.entity_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE p.id = CAST(:i AS uuid) AND p.state = 'discovered'
            """
        ),
        {"i": prospect_id},
    ).fetchone()
    if row is None:
        return None, None
    name, email, source_adapter, source_ref, data_basis, slug = row
    return (
        Candidate(display_name=name or "Unknown", website=None, email_raw=email,
                  source_adapter=source_adapter, source_ref=source_ref or {},
                  data_basis=data_basis),
        slug,
    )


def _store_evidence(conn: Connection, prospect_id: str, card: EvidenceCard) -> None:
    conn.execute(
        text("DELETE FROM evidence_facts WHERE prospect_id = CAST(:i AS uuid)"),
        {"i": prospect_id},
    )
    for fact in card.facts:
        conn.execute(
            text(
                """
                INSERT INTO evidence_facts (prospect_id, fact_type, content, source_url,
                    observed_at)
                VALUES (CAST(:p AS uuid), :ft, CAST(:c AS jsonb), :u, :o)
                """
            ),
            {"p": prospect_id, "ft": fact.fact_type, "c": json.dumps(fact.content),
             "u": fact.source_url, "o": fact.observed_at},
        )


def _load_card(conn: Connection, prospect_id: str) -> EvidenceCard:
    from open_reachout.core.interfaces import EvidenceFact

    rows = conn.execute(
        text(
            """
            SELECT id, fact_type, content, source_url, observed_at
            FROM evidence_facts WHERE prospect_id = CAST(:i AS uuid) ORDER BY observed_at
            """
        ),
        {"i": prospect_id},
    ).fetchall()
    facts = [
        EvidenceFact(
            fact_id=str(r[0]), fact_type=r[1],
            content=r[2] if isinstance(r[2], str) else json.dumps(r[2]),
            source_url=r[3], observed_at=r[4],
        )
        for r in rows
    ]
    return EvidenceCard(prospect_ref=prospect_id, facts=facts)


def _persona_and_candidate(
    conn: Connection, runtimes: dict[str, TenantRuntime], prospect_id: str, required_state: str
) -> tuple[TenantRuntime | None, PersonaSpec | None, Candidate | None]:
    row = conn.execute(
        text(
            """
            SELECT t.slug, p.persona_id, e.display_name, p.email_raw, p.source_ref
            FROM prospects p JOIN entities e ON e.id = p.entity_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE p.id = CAST(:i AS uuid) AND p.state = :s
            """
        ),
        {"i": prospect_id, "s": required_state},
    ).fetchone()
    if row is None:
        return None, None, None
    slug, persona_id, name, email, source_ref = row
    runtime = runtimes[slug]
    persona = next((p for p in runtime.config.personas if p.id == persona_id), None)
    candidate = Candidate(
        display_name=name or "Unknown", org_name=name, email_raw=email,
        source_adapter="db", source_ref=source_ref or {},
        data_basis="government_public",
    )
    return runtime, persona, candidate


def _load_for_qualify(conn, runtimes, prospect_id):  # noqa: ANN001
    runtime, persona, _ = _persona_and_candidate(conn, runtimes, prospect_id, "enriched")
    return runtime, persona, _load_card(conn, prospect_id)


def _load_for_compose(conn, runtimes, prospect_id):  # noqa: ANN001
    runtime, persona, candidate = _persona_and_candidate(
        conn, runtimes, prospect_id, "queued"
    )
    return runtime, persona, _load_card(conn, prospect_id), candidate


def runtime_for(conn: Connection, config: TenantConfig) -> TenantRuntime:
    """Build a TenantRuntime, ensuring the tenant row exists."""
    return TenantRuntime(
        config=config,
        tenant_id=_tenant_id(conn, config.tenant),
        validator_ctx=dryrun.validator_context(config),
    )


def seed_discovery(conn: Connection, runtime: TenantRuntime) -> int:
    """Enqueue a discover job per (persona, cohort). Returns jobs enqueued."""
    n = 0
    for persona in runtime.config.personas:
        for cohort in persona.cohorts:
            queue.enqueue(
                conn, "discover",
                {"tenant": runtime.config.tenant, "persona": persona.id, "cohort": cohort.id},
                idempotency_key=f"discover:{runtime.config.tenant}:{cohort.id}",
            )
            n += 1
    return n
