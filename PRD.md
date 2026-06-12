# PRD: Open Reachout — an Open-Source Agentic Outreach Framework

**Status:** Draft v2 · June 2026
**Change from v1:** Reframed from "internal backend for two businesses" to **a free, open-source library/framework**. The two businesses (a therapist directory and a three-sided music marketplace) are now **reference use cases** shipped as example configs — the first consumers of the library, not the product.
**Companion doc:** [`research/market-research-report.md`](research/market-research-report.md) — market evidence behind every major decision here.

---

## 1. Vision

**Open Reachout is the open-source framework for agentic outbound outreach** — the thing you reach for when your business depends on finding specific kinds of people on the open web and starting honest, compliant, personalized email conversations with them at a configured monthly volume.

You declare **personas** in config. The framework:

1. **Discovers prospects** matching those personas via pluggable source adapters (public registries, Google Places, directories, agentic web search/scraping) — not B2B contact databases.
2. **Enriches and qualifies** each prospect by reading their actual web presence into a cited Evidence Card.
3. **Composes and sends** personalized cold email with bounded drip follow-ups through your own sending-provider account.
4. **Handles replies agentically** — classifies intent, answers from your FAQ, sends links, calls your APIs, escalates to you when unsure.
5. **Experiments honestly** — Thompson-sampling bandits over message variants with guardrail metrics, plus pooled attribute learning, instead of fake "AI optimization."
6. **Proposes new cohorts** from outcome data on a research cadence — the closed loop nothing on the market closes.

Self-hosted, BYO API keys, compliance guardrails in the core and non-bypassable.

### Why open source, and why now (research summary)

- **Commercial tools are closed boxes at $250–$5,000/mo** (11x, Artisan, AiSDR, Agent Frank, Instantly's agents), all welded to B2B contact databases and B2B SaaS sales motions. None handles prospects who live on Google Maps, government registries, Bandcamp, or their own websites; none does outcome-driven cohort discovery; none exposes a verifiable experimentation loop.
- **The open-source shelf is empty.** The landscape scan found only toy/demo projects (open-sdr, brightdata/ai-sdr-bdr-agent, agentuity/agent-sdr, ChiragBellara/AI-SDR-Agent) — no production-grade OSS framework with a real pipeline, sending integration, statistics, or compliance posture. Open Reachout would be first.
- **The hard knowledge is encodable.** Deliverability rules (≤25/inbox/day, warmup, domain isolation, 0.1% complaint targets), CAN-SPAM completeness, suppression-first architecture, and low-volume statistics (bandits, not significance theater) are exactly the kind of expertise a framework can bake in so individual builders stop re-learning it by burning domains.
- **Author's forcing function:** two real marketplaces (Business A: Psychology Today competitor; Business B: bands/sound-techs/venues) get built *on* the library as `examples/`, keeping the abstractions honest.

## 2. Product Definition

**What it is:** a Python package (`open-reachout`) + CLI + optional read-only web dashboard. You run it on your own infrastructure (a VPS + Postgres), with your own keys for scraping, enrichment, LLM, and sending providers.

**What it is not:**
- Not a hosted SaaS (though the license must not preclude anyone offering one — see §10).
- Not a contact database. It ships zero data; it ships *adapters* to data you're entitled to use.
- Not a sending infrastructure. It drives Smartlead/Instantly/etc. via their APIs; it never speaks SMTP itself in v1.
- Not a spam cannon. Volume caps, suppression, and CAN-SPAM validators live in the core and cannot be disabled by config or plugins (see §8, §11).

**Primary user ("Operator"):** a technical founder/developer who can edit YAML, run a CLI, and read a weekly digest. Secondary: contributors writing adapters for new data sources and providers.

## 3. Goals & Non-Goals

### Goals

- G1: **Config-first.** A complete outreach program — personas, cohorts, sequences, experiments, budgets, compliance identity — is declarative, versionable YAML. `reachout validate && reachout run` is the whole interface.
- G2: **Pluggable everything at the edges.** Source adapters, enrichers, email finders, verifiers, sending providers, LLM backends, and reply actions are all interfaces with entry-point plugin registration. Core pipeline logic stays in the framework.
- G3: **Compliance and deliverability by construction.** The framework refuses to send non-compliant mail, over-budget mail, or mail to suppressed/unverified addresses — regardless of what configs or plugins ask for.
- G4: **Honest statistics built-in.** Bandit allocation, guardrail pausing, and pooled attribute learning ship in the core with sane defaults; no user should need to understand Thompson sampling to benefit from it.
- G5: **Agentic but governed.** Autonomy levels are explicit per capability (`off | propose | auto`): reply handling, variant generation, cohort discovery, budget shifts. Defaults are conservative; full autopilot is opt-in per capability.
- G6: **Cheap and inspectable.** Runs on one VPS + Postgres; every LLM call, scrape, and send is logged with cost; a full audit trail per prospect is one query away.
- G7: **Dogfooded.** The two reference use cases ship in `examples/` and are run in production by the author; framework releases are gated on the examples still working.

### Non-Goals (v1)

- NG1: Channels beyond email (LinkedIn, SMS, voice). The `Touch` model leaves room; no v1 implementation.
- NG2: Hosted/multi-customer SaaS, billing, user management. Single-operator, multi-tenant-config deployments only.
- NG3: Building marketplaces/CRMs. Conversion ends at a webhook/API call into *your* app.
- NG4: Non-US compliance regimes (CASL, GDPR/PECR). v1 targets US CAN-SPAM; the compliance module is regime-pluggable so contributors can add others.
- NG5: ToS-prohibited scraping. A hard-coded source denylist (Psychology Today, logged-in content, etc.) that config can extend but never shrink.
- NG6: Owning deliverability primitives (SMTP, warmup networks, IP pools). Always delegated to sending providers.

## 4. Architecture & Repository Shape

```
open-reachout/
├── core/                  # pipeline engine, domain model, state machines, queues
│   ├── models.py          # Tenant, Persona, Cohort, Prospect, Touch, Reply,
│   │                      # Campaign, Sequence, Experiment, Variant, Proposal
│   ├── pipeline.py        # discover → enrich → qualify → compose → send →
│   │                      # observe → classify → learn → expand
│   ├── budget.py          # monthly/cohort/inbox gates (non-bypassable)
│   ├── suppression.py     # global + tenant suppression (non-bypassable)
│   └── compliance/        # validators, kill switches, audit log, denylist
├── adapters/              # built-in implementations of the plugin interfaces
│   ├── sources/           # nppes, google_places, state_boards, bandsintown,
│   │                      # bandcamp, indie_on_the_move, web_research
│   ├── enrich/            # firecrawl, email waterfall (prospeo, findymail, hunter)
│   ├── verify/            # millionverifier, sender_bundled
│   ├── sending/           # smartlead, instantly  (SendingProvider interface)
│   └── llm/               # anthropic (default), openai-compatible
├── stats/                 # thompson sampling, guardrails, pooled attribute model
├── agents/                # qualifier, composer, reply_handler, discovery_agent
├── cli/                   # reachout init|validate|dry-run|run|report|approve
├── dashboard/             # optional read-only FastAPI + htmx views
├── examples/
│   ├── therapist-directory/   # Business A reference config (see Appendix A)
│   └── music-marketplace/     # Business B reference config (see Appendix B)
├── docs/                  # mkdocs: tutorial, interfaces, compliance, deliverability
└── tests/                 # incl. a full fake-provider end-to-end harness
```

**Runtime shape:** queue-driven pipeline; each stage is a worker over a Postgres-backed job queue (no Redis/Kafka — volumes are thousands of jobs/day). Postgres (+pgvector) is the only stateful dependency. Provider webhooks land on a small FastAPI app. Deploy = `docker compose up` (app, worker, Postgres) or a single VPS.

**Language: Python 3.12+.** Rationale: largest contributor pool for AI/data tooling, best ecosystem fit (pydantic for config/models, FastAPI for webhooks/dashboard), and the agent/LLM OSS community lives there. (Was an open question in v1; deciding it here — revisit only if a strong TS contributor base materializes.)

## 5. Plugin Interfaces (the contract surface)

All interfaces are small, typed (pydantic models in/out), and registered via Python entry points (`open_reachout.sources`, etc.), so third-party packages can ship adapters without forking.

```python
class SourceAdapter(Protocol):
    """Find raw candidates for a cohort. Must stamp provenance + cost."""
    def discover(self, cohort: CohortSpec, cursor: Cursor | None) -> DiscoverResult: ...

class Enricher(Protocol):
    """Candidate → EvidenceCard (structured facts + verbatim quotes + source URLs)."""
    def enrich(self, candidate: Candidate) -> EvidenceCard: ...

class EmailFinder(Protocol):
    def find(self, prospect: ProspectIdentity) -> EmailResult | None: ...   # waterfall-composable

class Verifier(Protocol):
    def verify(self, email: str) -> VerifyResult: ...

class SendingProvider(Protocol):
    """Wraps Smartlead/Instantly/etc. Owns mailboxes, warmup state, unsubscribe."""
    def send(self, message: OutboundMessage) -> SendReceipt: ...
    def mailbox_health(self) -> list[MailboxHealth]: ...
    def parse_webhook(self, payload: bytes) -> list[Event]: ...             # reply/bounce/complaint/unsub

class LLMBackend(Protocol):
    def complete(self, task: LLMTask) -> LLMResult: ...                     # qualify/compose/classify/research

class ReplyAction(Protocol):
    """Pre-authorized actions the reply agent may take: send_link, call_api, book_calendar…"""
    def execute(self, reply: Reply, ctx: TenantContext) -> ActionResult: ...

class ExperimentPolicy(Protocol):
    def select(self, experiment: Experiment) -> Variant: ...                # default: ThompsonSampling
    def update(self, experiment: Experiment, observation: Observation) -> None: ...
```

**Interface requirements:**
- FR-I.1: Every adapter call is an idempotent job with retries, rate limiting, cost accounting, and a dead-letter queue.
- FR-I.2: Adapters declare a **ToS/licensing self-description** (`data_basis: government_public | licensed | own_site_scrape | api_terms`) surfaced in docs and audit logs; candidates carry provenance forever.
- FR-I.3: A `FakeProvider` implementation of every interface ships in core, enabling the end-to-end test harness and `dry-run` mode with zero external calls.

## 6. The Core Loop & Domain Model

(Carried over from v1 — unchanged in substance, now framework-generic.)

**Domain model:** `Tenant → Persona → Cohort → Prospect → Touch/Reply`, plus `Campaign/Sequence/Experiment/Variant`, `SourceAdapter` registrations, `SuppressionList`, `Mailbox/SendingDomain`, and `Proposal` (agent suggestions awaiting operator decision). A tenant is one business/brand; one deployment runs many tenants with row-level isolation and **never-shared sending domains**.

**Prospect state machine:** `discovered → enriched → qualified → queued → contacted → engaged → converted`, with exits to `disqualified`, `unenrichable`, `bounced`, `declined`, `unsubscribed`, `no_response (90d cooldown, one re-eligibility)`. Every transition is an immutable audit event.

**Pipeline:** discover → enrich → qualify → compose → send → follow-up → observe → classify → learn → expand (cohort discovery), looping.

## 7. Functional Requirements (framework core)

### 7.1 Config & CLI

- FR-1.1: One YAML tree per deployment, pydantic-validated, atomic apply; `reachout validate` catches everything statically catchable (unknown adapters, budget > inbox capacity, missing compliance identity, sequence >3 follow-ups).
- FR-1.2: `reachout init` scaffolds a tenant interactively (incl. copying a reference example); `reachout dry-run --cohort X --n 100` runs the full pipeline through compose with FakeProviders or real ones (flag), writing would-send messages to a review file; `reachout approve` works the Proposal/escalation queue from the terminal; `reachout report` prints the weekly digest on demand.
- FR-1.3: Secrets via env vars only; config files contain no keys; `reachout doctor` checks provider connectivity/quotas/DNS (SPF/DKIM/DMARC) and warmup status.

### 7.2 Discovery, enrichment, qualification

- FR-2.1: Built-in adapters at v1: NPPES bulk-file ingestor, Google Places, generic state-board CSV/HTML ingestor, Bandsintown, Bandcamp, Indie on the Move (licensed), and `web_research` (Firecrawl/Tavily agentic search with cited results).
- FR-2.2: Hard-coded source **denylist** in core (psychologytoday.com, yelp.com scraping, anything behind login); config may extend, never shrink. Attempted denylist fetch = error + audit event.
- FR-2.3: Dedupe at ingest (fuzzy: name+domain+phone+address) against prospects and suppression. Source-quality scoring: qualification/bounce/conversion rates per adapter feed automatic throttling of weak sources.
- FR-2.4: Evidence Cards are the only personalization substrate (no hallucinated familiarity); LLM qualifier returns {qualified, disqualified, uncertain}+rationale; `uncertain → disqualified` by default (precision over recall; volume is capped anyway). Weekly sampled spot-check flow targets ≥90% operator agreement.

### 7.3 Composition, sending, sequencing

- FR-3.1: Composer = bandit-selected variant recipe + Evidence Card + persona voice. Non-bypassable post-generation validators: length cap; truthful, non-deceptive subject (WA CEMA / CA §17529.5 exposure, not just deliverability); physical address; unsubscribe text + provider one-click; claims grounded in tenant config only; no fake "Re:/Fwd:"; near-duplicate-content rejection across the sending day.
- FR-3.2: Budget gates at queue time, in order: tenant monthly cap → cohort cap → inbox daily cap (default ≤25) → mailbox health (warmup state, rolling bounce/complaint). Failure = stays queued; no partial sends.
- FR-3.3: Sequences: initial + ≤3 follow-ups (default 2), gaps ≥3 days, stop on reply/unsub/bounce/suppress. The 3-follow-up cap is a core constant, not config (complaint data: complaints triple by email 4).
- FR-3.4: Suppression check immediately before dispatch (in addition to provider-side); open-tracking pixels disabled; send windows respect prospect-local business hours.

### 7.4 Reply handling

- FR-4.1: LLM classification: `interested | question | not_interested | unsubscribe | out_of_office | wrong_person | hostile | other`; low-confidence and all `hostile` escalate.
- FR-4.2: Reply actions are pre-authorized per tenant via the `ReplyAction` registry: send signup/calendar link, call an operator API (e.g., `POST /invites`), answer **only** from the tenant FAQ knowledge base (one agentic exchange, then escalate), polite close + 12-month suppression, immediate unsubscribe suppression (legal limit 10 business days; framework does it in minutes).
- FR-4.3: Escalation queue with full thread context, daily digest email, SLA nag.

### 7.5 Experimentation (`stats/`)

- FR-5.1: One experiment surface at a time per cohort (subject, opener strategy, value-prop framing, CTA, send-time). Default policy: Thompson sampling on the configured success metric (default: positive reply).
- FR-5.2: Per-variant guardrails (complaint, unsub, bounce rates) pause a variant immediately regardless of reply performance.
- FR-5.3: Variants carry structured attribute tags; a hierarchical pooled model shares attribute effects across cohorts/tenants in a deployment, so 200-send cohorts borrow strength. (Docs must explain plainly why classic A/B significance is unattainable at cold-email volumes — ~2,200/arm for a 5%→7% lift — this is a teaching opportunity for the project.)
- FR-5.4: Agentic variant generation from winning attributes + reply-text mining, capped (`max_live_variants`, default 4); copy-level variants auto-approvable by config, value-prop-level changes always `propose`.

### 7.6 Cohort discovery agent

- FR-6.1: On a research cadence with a hard monthly LLM/search budget, mines outcomes + bounded web research → **Proposals**: new cohorts (with evidence links, size estimates, cost projections), budget shifts, value-prop deltas, opportunity flags. Declines are remembered 90 days.
- FR-6.2: Autonomy: `off | propose (default) | auto_launch_within_budget` (auto mode may launch new cohorts of *existing* personas inside a budget envelope; never new personas).

### 7.7 Compliance module (cross-cutting, non-bypassable)

- FR-7.1: Tenant compliance identity (physical address, brand, unsubscribe method) required before any send; validators per §7.3.
- FR-7.2: Suppression: tenant-level + deployment-global (global wins); export/import for portability.
- FR-7.3: Kill switches: rolling 7-day complaint >0.2% or bounce >3% per domain pauses that domain's campaigns + alerts. Provider postmaster polling daily.
- FR-7.4: Per-prospect audit export: every touch, source, provenance, and consent-relevant event — the honest answer to "how did you get my info?".
- FR-7.5: Regime plugin interface (`ComplianceRegime`) with `us_can_spam` as the only v1 implementation.

### 7.8 Observability

- FR-8.1: Weekly digest (email + `reachout report`): funnel per cohort, spend, experiment movers, proposals, escalations, deliverability health.
- FR-8.2: Cost ledger end-to-end: $/discovered, $/qualified, $/contacted, $/converted by cohort and source.
- FR-8.3: Optional dashboard (read-only) and a conversion webhook/API so the operator's app can report `converted` back.

## 8. Open-Source-Specific Requirements

- OSS-1: **License: Apache-2.0** (patent grant; permissive enough for commercial adoption including the author's own businesses; revisit AGPL only if cloud-capture becomes a real concern).
- OSS-2: **Responsible-use posture, enforced in code where possible:** compliance core non-bypassable (the project's answer to "isn't this a spam tool?"), conservative defaults, a prominent RESPONSIBLE_USE.md covering CAN-SPAM, provider AUPs, and why transactional ESPs are unsupported by design. We do not ship SMTP sending precisely so the deliverability incentives of the sending providers (account bans for spammers) remain in the loop.
- OSS-3: **Docs as a first-class deliverable:** quickstart (zero-to-dry-run in 15 minutes on FakeProviders), the two example walkthroughs, an interfaces guide for adapter authors, and a "Deliverability & Compliance 101" page distilling the research (this content is rare in OSS and is itself an adoption magnet).
- OSS-4: **Quality gates:** typed everywhere, CI on the fake-provider e2e harness, semver from 0.x with documented interface stability promises at 1.0, conventional releases to PyPI.
- OSS-5: **Contribution surface:** adapters are the designed contribution unit (new sources, new senders, new regimes); `CONTRIBUTING.md` + adapter cookiecutter template.
- OSS-6: **No telemetry.** Period. (Self-hosted outreach data is radioactive; not collecting it is a feature.)
- OSS-7: **Security:** secrets only via env, webhook signature verification per provider, `SECURITY.md` with a disclosure contact.

## 9. Success Metrics

**Library health:**
- Zero-to-dry-run in ≤15 minutes for a new user following the quickstart (tested on strangers).
- Fake-provider e2e suite green on every release; both `examples/` run against each release (G7).
- ≥3 community-contributed adapters within 6 months of 1.0 (signal, not goal).
- GitHub issues from real deployments (the honest adoption metric).

**Dogfood validation (via the examples, run by the author):**
- Deliverability: complaint <0.1%, bounce <2%.
- Reply ≥6% by month 3 (market avg 5.8%); positive-reply ≥2.5%; reply→conversion ≥30%.
- Operator time <4 hrs/week/tenant — the "fully agentic" promise, measured.
- ≥1 bandit-adopted variant improvement and ≥2 discovery proposals (≥25% accepted) per month per tenant.
- Business targets (90 days from first send): A — 100 claimed listings across 2 states; B — 30 venues + 60 bands in one metro, ≥10 booking requests.

## 10. Milestones

- **M0 — Skeleton (wk 1–2):** repo scaffolding, pydantic config + `validate`, domain model + migrations, suppression service, FakeProviders, e2e harness, Apache-2.0 + RESPONSIBLE_USE.md. *Parallel calendar-critical task: buy example-tenant domains, start 3-week warmup.*
- **M1 — Pipeline to dry-run (wk 3–5):** NPPES + Google Places + web_research adapters, Firecrawl enrichment, email waterfall + verification, qualifier, composer + validators, `reachout dry-run` end-to-end. Exit: 100 would-send emails reviewed and quality-approved for each example.
- **M2 — Live sends (wk 6–8):** Smartlead adapter (Instantly stub), budget gates, sequencing, webhooks, reply classifier + escalation, agentic actions for `interested`/`unsubscribe` only. Exit: 300 prospects contacted across both examples, zero compliance defects, kill switches fire in test.
- **M3 — Learning loop (wk 9–11):** Thompson allocation, guardrail pausing, attribute tagging + pooled model v0, digest + report, dashboard v0. Exit: first bandit-driven variant promotion with documented posterior.
- **M4 — Discovery + 0.1 release (wk 12–14):** discovery agent (`propose` mode), Proposals/approve flow, source-quality throttling, docs site, PyPI `0.1.0`, public repo. Exit: first agent-discovered cohort approved and live; quickstart tested on an outsider.
- **Post-0.1:** remaining reply intents, Instantly adapter complete, `auto_launch_within_budget`, more state boards, community adapter program, 1.0 interface freeze.

## 11. Risks & Mitigations

| # | Risk | L/I | Mitigation |
|---|---|---|---|
| R-1 | **Abuse: the framework is used to spam** | M / H (reputational) | Non-bypassable compliance core; no SMTP; sending only via providers whose AUPs ban abuse; conservative defaults; RESPONSIBLE_USE.md; refuse features whose only use is evasion (e.g., spintax, tracking-pixel cloaking). |
| R-2 | **Deliverability collapse for users** (domains burned by misconfiguration) | M / H | `reachout doctor` DNS/warmup checks; hard caps; kill switches; "Deliverability 101" docs; FakeProvider dry-runs as the default first experience. |
| R-3 | **Framework-itis: abstractions before evidence** | M / M | G7 dogfooding — every interface must be exercised by both examples before 1.0; adapters not needed by an example wait for a contributor with a real use. |
| R-4 | **LLM reply errors** (wrong answers, tone misses with sensitive audiences) | M / M | FAQ-grounded answers only; one agentic exchange then escalate; hostile always escalates; weekly sampled QA flow in core. |
| R-5 | **The 11x quality trap** (volume up, relevance down) | M / H | Volume hard caps; precision-biased qualifier; conversion as the headline metric in every report. |
| R-6 | **Bandit converges on guardrail-risky copy** | L / M | Per-variant guardrail pausing; deceptive-subject validator non-bypassable. |
| R-7 | **Source ToS/legal drift** (Places caching, Bandsintown licensing) | M / L | Adapter `data_basis` declarations; provenance everywhere; denylist; prefer government (NPPES) + licensed (IOTM) sources in examples. |
| R-8 | **Email-finder coverage weak for non-B2B ICPs** | H / M | Own-website scrape is waterfall step 1; track contactable% per source; oversample discovery. |
| R-9 | **Maintainer bandwidth: library + two businesses, one founder** | H / M | The examples ARE the businesses (no duplicate work); <4 hrs/wk operator metric; community contribution surface designed around adapters; scope ruthlessness via NG list. |
| R-10 | **Demand side of the example marketplaces** (this recruits supply; clients/audiences are separate) | H / H (business-level) | Out of framework scope; flagged in both example READMEs so the configs aren't mistaken for complete go-to-market strategies. |

## 12. Open Questions

1. Project name/PyPI availability check (`open-reachout` vs `reachout`).
2. Smartlead vs Instantly as the *first* sending adapter — spike both APIs in M1 wk 1 (Smartlead Pro default per research).
3. Dashboard: ship in core vs separate package (`open-reachout-dashboard`)? Default: core but optional-extra install.
4. Example-tenant launch parameters (states/metro, brand names — needed M0 for domain warmup; defaults: TX+GA, Austin).
5. Pooled attribute model v0 scope: simple hierarchical Beta-Binomial vs logistic with partial pooling (start simplest).
6. Whether/when to accept a hosted-service contributor track (park until post-1.0).

---

## Appendix A — Reference use case: `examples/therapist-directory`

A Psychology Today competitor recruiting individual therapists in private practice. Demonstrates: government-registry sourcing, high-sensitivity voice, trust-first compliance.

- **Persona:** `solo_therapist` — US licensed therapist/psychologist, solo or 2–3-person practice, own website, likely PT-dissatisfied (documented referral collapse: ~8–15 inquiries/mo in 2020 → ~1–3 in 2026).
- **Sources:** NPPES bulk file (taxonomy-filtered; free), state boards, own-website enrichment. **Never** Psychology Today (denylisted). **CareDash rule** baked into the example's docs: registry data feeds private outreach only — never public profile pre-population (that pattern ended CareDash via APA cease-and-desist, Feb 2023).
- **Config highlights:** 1,200 prospects/mo across 2 state×license cohorts; initial + 2 follow-ups (gaps 4/7d); provenance transparency in-message ("found you via the public NPI registry and your practice site"); experiments start on value-prop framing ("free until first client" vs "anti-platform transparency").
- **Conversion:** claimed listing via signup link; `ReplyAction: call_api POST /invites`.

## Appendix B — Reference use case: `examples/music-marketplace`

A three-sided membership marketplace (bands ↔ sound techs ↔ small venues). Demonstrates: Places-based local-business discovery, multi-persona tenancy, demand-signal mining.

- **Personas:** `small_venue` (first — anchors liquidity; venues anchored at free by GigFinesse), `gigging_band` (membership willingness proven by Indie on the Move at $6.99–34.99/mo), `sound_tech` (**deferred**: riskiest assumption — $300–650/band gigs rarely budget a tech; launch only when venue replies show demand, which the discovery agent is configured to mine for).
- **Sources:** Google Places (live-music venues; discovery only, contacts from venues' own sites per ToS), Indie on the Move (licensed), Bandsintown/Bandcamp + venue calendars for bands.
- **Config highlights:** one metro; venues ~200/mo + bands ~300/mo; venue outreach references the venue's actual events calendar in the Evidence Card; experiments start on CTA type (profile link vs reply-to-chat).
- **Conversion:** profile created / first booking request, reported via the conversion webhook.

*All market figures and vendor claims are sourced and verification-flagged in [`research/market-research-report.md`](research/market-research-report.md).*
