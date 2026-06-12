# PRD: Open Reachout — Unified Agentic Outreach Backend

**Status:** Draft v1 · June 2026
**Companion doc:** [`research/market-research-report.md`](research/market-research-report.md) — market evidence behind every major decision here.

---

## 1. Vision

A single, config-driven, agentic outreach backend that acquires marketplace supply (and demand) by:

1. **Defining personas** in config (e.g., "solo-practice psychologist in Texas," "cover band gigging within 50mi of Austin," "brewery with a weekend live-music series").
2. **Autonomously discovering prospects** matching those personas via web search, public registries, directories, and scraping — not B2B contact databases, which don't contain these people.
3. **Reaching out by email** with deeply personalized first-touches and bounded drip follow-ups, at a configured monthly volume.
4. **Handling replies agentically** — classifying intent, answering questions, sending signup links, calling simple APIs (create account, book calendar slot), and escalating to a human when needed.
5. **Experimenting continuously** — bandit-allocated message variants, pooled learning across cohorts — to find what works.
6. **Discovering new cohorts and opportunities** from outcome data, proposing (or with permission, launching) expansion into new segments.

Set the config, set the monthly budget, and the system runs.

### Why this should exist (research summary)

- Autonomous persona-based outreach exists for B2B SaaS sales (11x, Artisan, AiSDR, Agent Frank — $250–$5,000/mo) but **no product does autonomous cohort discovery, none exposes a real experimentation loop, and none targets marketplace supply acquisition**. Prospects like therapists, bands, and cafes are not in ZoomInfo-style databases; every incumbent assumes they are.
- The category's known failure mode is quality, not features (11x churn scandal; 847 emails → 1 meeting). Our counter-position: **lower volume, deeper per-prospect research, honest statistics.**
- It serves two real businesses with one codebase:
  - **Business A ("TheraDirectory")** — a Psychology Today competitor. Supply = individual therapists in private practice. Tailwind: PT referrals are collapsing (~8–15 inquiries/mo in 2020 → ~1–3 in 2026) and therapists are loudly unhappy — a receptive audience.
  - **Business B ("StageMatch")** — a three-sided membership marketplace: bands ↔ sound techs ↔ small venues (cafes, bars, breweries, wineries). No three-sided competitor exists; the sound-tech leg is whitespace (and the riskiest assumption — see §12).

## 2. Goals & Non-Goals

### Goals

- G1: One backend, N tenants. Business A and B run as isolated tenants sharing all code; adding a third business = writing config + (maybe) a source adapter.
- G2: Full-cycle autonomy with human gates where stakes are high: prospect discovery, enrichment, sending, follow-ups, and reply triage run unattended; new-cohort launches and unusual replies require approval (configurable per tenant).
- G3: Monthly volume budgeting — "contact ≤1,500 new prospects/month for tenant A" enforced system-wide, with per-inbox daily caps (≤30) enforced beneath it.
- G4: Closed learning loop — every send is an observation; variant allocation uses Thompson sampling; message-attribute learnings pool across cohorts; cohort-level performance feeds discovery.
- G5: Compliance by construction — CAN-SPAM-complete messages, suppression-first sending, complaint-rate kill switches, auditable history of every touch.
- G6: Cheap to run — target <$400/mo infra at 1,500 sends/mo (excluding LLM costs), per the build-vs-buy analysis.

### Non-Goals (v1)

- NG1: Building the marketplaces themselves (directory UI, booking flows, payments). This system *feeds* them; it ends at "prospect converted = signed up / booked a call."
- NG2: Channels beyond email (LinkedIn, SMS, calls). Email-first; the data model leaves room for channels later.
- NG3: Building sending/deliverability infrastructure. We sit on Smartlead or Instantly APIs (decision in §7.5).
- NG4: Fully autonomous cohort *launch* by default. The discovery agent proposes; humans approve. An opt-in "auto-launch within guardrails" mode is v2.
- NG5: International sending. US-only (CAN-SPAM regime). CASL/GDPR are explicitly out of scope until revisited.
- NG6: Scraping ToS-prohibited sources. Psychology Today, Yelp, logged-in content: never. Enforced by a source blocklist, not by convention.

## 3. Users & Permissions

| Role | Description | Can do |
|---|---|---|
| **Operator** (the founder) | Configures tenants, personas, budgets; reviews proposals; handles escalations | Everything |
| **Reviewer** (future contractor) | Works the escalation queue, approves copy variants | Approve/reply within a tenant |
| **The agent** | The system itself | Everything pre-authorized by config; nothing on the deny list |

Single-operator ergonomics matter: every approval surface must work from a daily email digest / simple web queue, not require a dashboard babysitter.

## 4. Domain Model

```
Tenant (Business A, Business B)
 └── Persona            — a named ICP definition + value prop + voice (config)
      └── Cohort        — a concrete targeting slice of a persona
                          (e.g., persona=therapist, cohort="LMFTs, Austin TX, solo practice")
           └── Prospect — one human/org: identity, evidence, contact info, state machine
                └── Touch        — one outbound email (or future channel event)
                └── Reply        — one inbound message + classification
 └── Campaign           — cohort × sequence × experiment policy × volume budget
      └── Sequence      — ordered steps (initial + ≤3 follow-ups) with stop conditions
      └── Experiment    — bandit over Variants on a declared surface (subject, opener, CTA, ...)
           └── Variant  — a message recipe (template + personalization directives)
 └── SourceAdapter      — a prospect discovery integration (NPPES, Google Places, ...)
 └── SuppressionList    — tenant-global + system-global; email + domain level
 └── Mailbox / SendingDomain — managed sending identities with health state
 └── Proposal           — agent-generated suggestion (new cohort, new variant family,
                          budget shift) awaiting operator decision
```

**Prospect state machine:**

```
discovered → enriched → qualified → queued → contacted → engaged → converted
                │            │                   │           ├→ declined (suppress)
                │            └→ disqualified     │           ├→ unsubscribed (suppress, ≤10 biz days hard limit)
                └→ unenrichable                  └→ bounced (suppress + count against source quality)
                                                             └→ no_response (cooldown 90d, then re-eligible once)
```

Every transition is an immutable audit event (who/what/when/why), satisfying G5.

## 5. The Core Loop

```
        ┌─────────────────────────────────────────────────────────────┐
        │  1 DISCOVER   source adapters find raw candidates           │
        │  2 ENRICH     scrape candidate's own web presence;          │
        │               find + verify email (waterfall)               │
        │  3 QUALIFY    LLM scores candidate vs persona definition;   │
        │               builds an Evidence Card                       │
        │  4 COMPOSE    variant selected by bandit; LLM writes        │
        │               personalized email from Evidence Card         │
        │  5 SEND       via sending provider; budget + health gates   │
        │  6 FOLLOW UP  drip steps until reply/stop condition         │
        │  7 OBSERVE    delivery, bounce, reply, complaint webhooks   │
        │  8 CLASSIFY   LLM intent classification; act or escalate    │
        │  9 LEARN      update bandit posteriors + attribute model    │
        │ 10 EXPAND     discovery agent mines outcomes → Proposals    │
        └───────────────────────────────────── loops back to 1 ──────┘
```

## 6. Functional Requirements

### 6.1 Configuration (the product surface)

Everything an operator controls lives in declarative, version-controlled config (YAML in this repo, validated on load).

```yaml
tenant: theradirectory
sending:
  monthly_new_prospects: 1200        # hard cap, FR-5.3
  max_daily_per_inbox: 25
  inboxes: [outreach@try-theradirectory.com, hello@get-theradirectory.com, ...]
  send_window: { days: mon-fri, hours: "08:30-17:30", timezone: prospect_local }
compliance:
  physical_address: "123 Main St #400, Austin TX 78701"
  unsubscribe: one_click_plus_text
  kill_switch: { complaint_rate: 0.2%, bounce_rate: 3% }   # below Google's 0.3% ceiling
personas:
  - id: solo_therapist
    description: >
      Licensed therapist/psychologist in US private practice, solo or 2-3 person
      group, currently listing on directories, likely dissatisfied with
      Psychology Today referral volume.
    evidence_signals: [has_own_website, lists_self_pay_rates, pt_profile_exists, solo_npi_org]
    value_prop: >
      Free-until-first-client directory listing; individual practitioners only —
      no VC-platform bulk profiles; transparent flat pricing afterward.
    voice: { tone: warm_professional, length: under_150_words, reading_level: plain }
    sequence: { steps: 3, gaps_days: [4, 7] }     # initial + 2 follow-ups (Belkins complaint data)
    cohorts:
      - id: tx_lmft_2026q3
        filters: { state: TX, taxonomy: [106H00000X], practice_size: solo }
        monthly_budget: 400
    experiments:
      - surface: subject_line
        policy: thompson
        success_metric: positive_reply
        guardrails: { complaint_rate: 0.15%, unsub_rate: 1% }
discovery_agent:
  mode: propose_only          # propose_only | auto_launch_within_budget (v2)
  research_cadence: weekly
  monthly_research_budget_usd: 25
```

- FR-1.1: Config changes are diffable, reviewable, and atomic; a bad config never half-applies.
- FR-1.2: Per-persona `voice` and `value_prop` drive generation; no free-floating prompts buried in code.
- FR-1.3: A `dry_run: true` tenant mode runs the whole loop through step 4 (compose) and stops, writing would-have-sent messages to a review file — first-class, because it's how every new cohort gets smoke-tested.

### 6.2 Discovery (source adapters)

- FR-2.1: `SourceAdapter` interface: `discover(cohort_filters) → [RawCandidate]`, with per-adapter rate limits, cost accounting, and provenance stamped on every candidate.
- FR-2.2: v1 adapters:
  - **NPPES/NPI** (Business A): ingest the public dissemination file (weekly refresh); filter by taxonomy + address; free. *Bright-line rule (CareDash): NPI data feeds private outreach targeting only — never public profiles.*
  - **State licensing boards** (Business A): per-state CSV/HTML ingestors, added incrementally.
  - **Google Places** (Business B venues): text/nearby search for cafes/bars/breweries/wineries with live-music signals; respect no-long-term-caching ToS — store only place_id + our own follow-up scrape of the venue's website.
  - **Indie on the Move** (Business B venues): licensed data via Premium/Deluxe ($6.99–34.99/mo); optionally pitch through their QuickPitch ($0.25/venue) as a parallel channel.
  - **Bandsintown/Bandcamp/venue calendars** (Business B bands): bands that played similar venues recently; Bandcamp pages often publish booking emails.
  - **Agentic web search** (all): Firecrawl/Tavily-driven "find live-sound engineers serving the Austin metro" style open research, producing candidates with cited evidence URLs.
- FR-2.3: **Source blocklist** (NG6) checked before any fetch: psychologytoday.com, yelp.com, anything requiring login. Hard-coded deny, config can only extend it.
- FR-2.4: Dedupe at ingest against existing prospects (fuzzy: name + domain + phone + address) and against all suppression lists.
- FR-2.5: Source quality scoring: each adapter's candidates are tracked through to qualification rate, bounce rate, and conversion; low-quality sources get throttled automatically.

### 6.3 Enrichment & contact finding

- FR-3.1: For each candidate, scrape their own web presence (practice site, EPK, venue site, linked socials) via Firecrawl into an **Evidence Card**: structured facts + verbatim quotes + source URLs. The Evidence Card is the only thing the composer may personalize from — no hallucinated familiarity.
- FR-3.2: Email waterfall: (1) email found on their own site/EPK → (2) Prospeo → (3) FindyMail → (4) Hunter. Stop at first verified hit; record provider + cost.
- FR-3.3: Verification (MillionVerifier or sender-bundled) before queueing; predicted bounce >threshold → `unenrichable`, never sent.
- FR-3.4: Per-prospect enrichment cost ledger; cohort-level CAC-to-date always queryable.

### 6.4 Qualification

- FR-4.1: LLM qualifier scores Evidence Card vs persona `evidence_signals` → {qualified, disqualified, uncertain} + rationale. `uncertain` defaults to disqualified (precision over recall — volume is capped anyway, so spend it on good fits).
- FR-4.2: Qualification rationale stored verbatim; sampled weekly for operator spot-checks (target: ≥90% operator agreement; below that, the qualifier prompt goes back into development).

### 6.5 Composition & sending

- FR-5.1: Composer = variant recipe (from the experiment policy, §6.7) + Evidence Card + persona voice → email. Hard constraints validated post-generation: length cap, no deceptive subject (litigation, not just deliverability — WA CEMA / CA §17529.5), physical address present, unsubscribe present, no claims not grounded in tenant config, no fake "Re:".
- FR-5.2: Per-prospect uniqueness check: near-duplicate content across a sending day is rejected and recomposed (AI-fingerprinting deliverability risk).
- FR-5.3: Budget gates, evaluated at queue time: monthly tenant cap → cohort cap → inbox daily cap → mailbox health (warmup state, recent bounce/complaint). Any gate failure = stays queued, never partial-sends.
- FR-5.4: Sending via provider API (§7.5) with provider-side unsubscribe handling **plus** our own suppression check immediately before dispatch.
- FR-5.5: Open-tracking pixels disabled; success metrics are replies and clicks on our links only.

### 6.6 Sequencing & reply handling

- FR-6.1: Sequences: initial + ≤3 follow-ups (default 2), gaps ≥3 days, all stop on: any reply, unsubscribe, bounce, suppression, or sequence end. Cap is config-lowerable but not raisable past 3 (Belkins: complaints triple by email 4).
- FR-6.2: Reply classification (LLM): `interested | question | not_interested | unsubscribe | out_of_office | wrong_person | hostile | other`. Anything below a confidence threshold, plus all `hostile`, escalates to the human queue.
- FR-6.3: Agentic reply actions (pre-authorized per tenant config):
  - `interested` → send signup link / calendar link; optionally call tenant API (`POST /invites`) to pre-create the account; mark `engaged`.
  - `question` → answer **only** from the tenant FAQ knowledge base; one agentic exchange max, then escalate. Never improvise pricing, legal, or clinical claims.
  - `not_interested` → polite close, suppress 12 months.
  - `unsubscribe` (explicit or one-click) → immediate suppression (system enforces the ≤10-business-day legal limit as "≤1 hour" in practice).
  - `wrong_person` → suppress address, optionally re-enrich prospect.
- FR-6.4: Every agentic reply is sent from the same thread/mailbox, plain-text-styled, and logged with the classification + rationale.
- FR-6.5: Human escalation queue with full thread context; daily digest email; SLA target <24h (human's job, but the system nags).

### 6.7 Experimentation engine (differentiator #1)

- FR-7.1: Experiments declare a **surface** (subject line, opening line strategy, value-prop framing, CTA type, send time) and run **one surface at a time per cohort** — low volume cannot support factorial designs.
- FR-7.2: Allocation: **Thompson sampling** over variants, success = configured metric (default: positive reply), updated on every observation. No fixed-split A/B and no "statistical significance" theater — at 1,500 sends/mo, classic significance needs ~2,200/arm and will never arrive (verified math, see research report §3).
- FR-7.3: **Guardrail metrics** evaluated per variant: complaint rate, unsubscribe rate, bounce rate. A variant breaching guardrails is paused immediately regardless of reply performance.
- FR-7.4: **Pooled attribute learning:** variants are tagged with structured attributes (length bucket, question-vs-statement subject, social-proof present, etc.); a hierarchical model pools attribute effects across cohorts and tenants so a 200-send cohort borrows strength from the whole system's history. This is the honest version of what incumbents fake.
- FR-7.5: Variant generation: the agent proposes new variants from (a) top-performing attributes, (b) reply-text mining ("what did interested people respond to?"), capped by `max_live_variants` (default 4/cohort). New variants enter as Proposals (auto-approvable via config for copy-level changes; value-prop-level changes always need operator approval).
- FR-7.6: Experiment ledger: every variant's full text, attributes, posterior, and outcome history is queryable; the weekly report shows current best/worst with examples.

### 6.8 Cohort discovery agent (differentiator #2)

- FR-8.1: On `research_cadence`, the agent: analyzes per-cohort performance; runs bounded web research (budgeted, §6.1) into adjacent segments, geographies, and seasonal opportunities; and emits **Proposals**, e.g.:
  - "TX LMFT cohort: 9.1% positive-reply vs 4.2% for psychologists. Propose cohort `ga_lmft` (similar PT-pricing complaints found on r/therapists; ~3,100 NPPES records)."
  - "12 venue replies mention needing 'someone to run sound for open-mic night' — supports launching the sound-tech persona in Austin first."
  - "Wineries show 2x reply rate of bars; propose shifting 200 sends/mo of budget."
- FR-8.2: Every proposal carries: evidence links, estimated cohort size, projected cost, suggested sequence/value-prop deltas, and a one-click approve/decline. Declines are remembered (don't re-propose for 90 days).
- FR-8.3: Modes: `propose_only` (v1 default) and `auto_launch_within_budget` (v2; may launch cohorts whose persona already exists, within a budget envelope, never new personas).
- FR-8.4: The agent also performs **opportunity scanning** per tenant (new competitor directory launches, PT pricing changes, local music-scene news) and includes findings in the weekly digest.

### 6.9 Compliance module (cross-cutting)

- FR-9.1: Message validators (§6.5) are non-bypassable, including by the agent.
- FR-9.2: Suppression: tenant-global + system-global (a person who unsubscribed from tenant A is never contacted by tenant B at the same address unless independently discovered AND the system-global list is clean for them — default: system-global suppression wins).
- FR-9.3: Kill switches: complaint rate >0.2% or bounce >3% (rolling 7-day, per domain) pauses the domain's campaigns and alerts the operator. Provider postmaster data polled daily.
- FR-9.4: Full per-prospect audit trail exportable (every touch, source, consent-relevant event) — both for legal defense and for tenant trust ("how did you get my info?" gets an honest, instant answer: "public NPI registry + your practice website").
- FR-9.5: A documented data-provenance policy embedded in outreach: identify who we are, why we're reaching out, where we found them. (Therapists are sensitized post-CareDash; transparency is also the brand.)

### 6.10 Observability & reporting

- FR-10.1: Weekly operator digest (email): funnel per cohort (discovered → ... → converted), spend, experiment movers, proposals pending, escalations pending, deliverability health.
- FR-10.2: Live dashboard (simple, read-only web) for the same data + prospect search.
- FR-10.3: Cost accounting end-to-end: $/discovered, $/qualified, $/contacted, $/converted, by cohort and source.
- FR-10.4: Alerting: kill-switch trips, provider API failures, budget 80%/100%, escalation SLA breaches.

## 7. Architecture

### 7.1 Shape

Queue-driven pipeline; each loop stage (§5) is a worker consuming from and producing to durable queues. Single Postgres as source of truth. All external calls (scraping, enrichment, LLM, sending) are idempotent jobs with retry + dead-letter.

```
[Scheduler/cron] → discover.q → [Discovery workers + SourceAdapters]
                  → enrich.q  → [Firecrawl/email-waterfall workers]
                  → qualify.q → [LLM qualifier]
                  → compose.q → [Bandit selector + LLM composer + validators]
                  → send.q    → [Budget gate → Smartlead/Instantly API]
   provider webhooks → events.q → [Reply classifier / bounce / complaint handlers]
                                → [Learning updater (posteriors, attribute model)]
   weekly cron → [Discovery agent (research) → Proposals]
   Postgres ← everything    Object store ← raw scrapes/Evidence Cards
```

### 7.2 Tech selections

| Concern | Choice | Rationale |
|---|---|---|
| Language/runtime | TypeScript on Node (or Python — operator's preference; decide before M1) | Single-language pipeline + good SDK coverage for all chosen vendors |
| DB | Postgres (+ pgvector for reply/evidence similarity) | One database for everything at this scale |
| Queue | Postgres-backed job queue (e.g., Graphile Worker / pg-boss) | No extra infra; volumes are tiny (thousands of jobs/day) |
| LLM | Claude API — Sonnet-class for compose/classify/qualify; Opus/Fable-class for weekly discovery research | Cost/quality split; discovery is low-frequency, high-reasoning |
| Scraping | Firecrawl (primary) + Apify actors (NPPES, Bandsintown) + Tavily (agent search) | Per build-vs-buy analysis |
| Email finding | Prospeo → FindyMail → Hunter waterfall | ~$0.01–0.05/email vs Clay's $0.14–0.67+ |
| Verification | MillionVerifier (or sender-bundled) | Cheapest credible |
| Sending | **Smartlead Pro ($94/mo)** primary candidate; Instantly fallback (§7.5) | Unlimited mailboxes, API, warmup, webhooks |
| Hosting | One small VPS or Fly.io/Render app + managed Postgres | <$50/mo |

### 7.3 Tenancy

Row-level tenancy (`tenant_id` on everything) with per-tenant config, budgets, suppression scoping per FR-9.2, separate sending domains/inboxes per tenant (never shared — one tenant's deliverability incident must not burn the other).

### 7.4 Sending identity plan (per tenant)

- 2 lookalike secondary domains (e.g., `try-<brand>.com`, `get-<brand>.com`), never the primary.
- 2–3 mailboxes per domain → 4–6 inboxes/tenant; ≥21-day warmup before first cold send; ≤25 cold/day each → ~2,000–3,000 sends/mo ceiling per tenant, comfortably above the 1,200–1,500 budget.
- SPF+DKIM+DMARC from day one (provider-assisted), even though we're under the 5K/day bulk threshold — it's also Microsoft's requirement and table stakes.

### 7.5 Open decision: Smartlead vs Instantly

Smartlead Pro: better API surface and webhook granularity, bundled verification credits; full API requires Pro tier. Instantly: cheaper entry, strong deliverability tooling, but API and the AI-agent features overlap confusingly with what we're building. **Default: Smartlead Pro; spike both APIs in M1 week 1 and lock the choice.** Abstraction layer (`SendingProvider` interface) regardless, so switching costs stay low.

## 8. Tenant Configurations at Launch

### 8.1 Business A — TheraDirectory

| | |
|---|---|
| Personas | `solo_therapist` (v1). Later: group-practice owner, new-licensee. |
| First cohorts | 2 states × 2 license types (e.g., TX/GA × LMFT/LPC), ~400/mo each |
| Sources | NPPES (primary), state boards, practice-website scrape |
| Value prop (testable) | "Free until your first client" individual-practitioner directory; anti-platform positioning; transparent flat pricing after |
| Conversion event | Claimed listing (signup link) |
| Sequence | Initial + 2 follow-ups, gaps 4/7 days |
| Sensitivities | Post-CareDash trust: provenance transparency in-message; extra-conservative voice; never imply an existing profile |

### 8.2 Business B — StageMatch

| | |
|---|---|
| Personas | `small_venue` (first — they anchor liquidity), `gigging_band`, `sound_tech` (deferred until venue-side demand evidence, see R-2) |
| First cohorts | 1 metro (e.g., Austin): venues ~200/mo, bands ~300/mo |
| Sources | Google Places + venue-site scrape, Indie on the Move (licensed), Bandsintown/Bandcamp |
| Value prop (testable) | Venues: free, curated local acts w/ booking workflow (GigFinesse anchors venues at free). Bands: membership ~$7–15/mo for venue access + gig tools (IOTM proves willingness). Techs: TBD pending demand validation |
| Conversion event | Profile created / first booking request |
| Sequence | Initial + 2 follow-ups; venue outreach references their actual events calendar |

## 9. Success Metrics

**System (per tenant, steady state):**

| Metric | Target | Baseline rationale |
|---|---|---|
| Deliverability: complaint rate | <0.1% | Google target threshold |
| Bounce rate | <2% | Verification working |
| Reply rate | ≥6% by month 3 | 5.8% market avg; personalization should beat it |
| Positive-reply rate | ≥2.5% | ~40% of replies positive is typical when targeting is good |
| Reply→conversion | ≥30% | Agentic handling + low-friction signup |
| Human time | <4 hrs/week/tenant | The "fully agentic" promise, measured |
| Cost per conversion | <$25 (A), <$15 (B) | Infra+LLM+data / conversions |
| Experiment learning | ≥1 adopted winning variant change/month/tenant | Loop is actually learning |
| Discovery agent | ≥2 proposals/month, ≥25% operator-accepted | Proposals are useful, not noise |

**Business validation (what the system exists to prove):**
- A: 100 claimed listings in 2 states within 90 days of first send.
- B: 30 venues + 60 bands in one metro within 90 days; ≥10 booking requests flowing.

## 10. Milestones

**M0 — Foundations (wk 1–2):** repo scaffolding, Postgres schema for §4, config loader + validation, suppression service, provider spike → lock Smartlead/Instantly, buy domains, start warmup (calendar-critical: 3 wks).
**M1 — Pipeline to dry-run (wk 3–5):** NPPES + Google Places adapters, Evidence Card enrichment, email waterfall + verification, qualifier, composer + validators, full `dry_run` for one cohort/tenant. Exit: operator reviews 100 would-be emails and approves quality.
**M2 — First live sends (wk 6–8):** budget gates, sequencing, webhooks, reply classifier + escalation queue, agentic replies for `interested`/`unsubscribe` only (others escalate). Exit: 300 prospects contacted across both tenants, zero compliance defects, kill switches tested.
**M3 — Learning loop (wk 9–11):** Thompson allocation, guardrail pausing, attribute tagging + pooled model v0, weekly digest, dashboard. Exit: first bandit-driven variant promotion with documented lift posterior.
**M4 — Discovery agent (wk 12–14):** weekly research job, Proposals + approval flow, source-quality scoring, opportunity scanning. Exit: first operator-approved agent-discovered cohort goes live.
**v2 (later):** `auto_launch_within_budget`, remaining agentic reply intents, sound-tech persona launch (gated on R-2 evidence), third-tenant onboarding kit, LinkedIn channel exploration.

## 11. Costs (steady state, both tenants, ~2,500 sends/mo total)

| Item | $/mo |
|---|---|
| Smartlead Pro | 94 |
| Domains+inboxes (2 tenants × ~5) | ~70 |
| Firecrawl + Apify + Tavily | ~60 |
| Email finding + verification | ~50 |
| Indie on the Move Deluxe | 35 |
| Hosting + Postgres | ~40 |
| LLM (compose/classify ~Sonnet, weekly research ~Opus) | ~80–150 |
| **Total** | **~$430–500** |

## 12. Risks & Mitigations

| # | Risk | Likelihood/Impact | Mitigation |
|---|---|---|---|
| R-1 | **Deliverability collapse** (domains burned) | M / H | Secondary domains only; horizontal scaling; conservative caps; kill switches; tracking pixels off; per-prospect unique copy (FR-5.2). Burned domain = abandon, rotate (~$15). |
| R-2 | **Sound-tech leg has no demand** at cafe price points ($300–650/band gigs rarely budget a tech) | H / M (for Biz B) | Persona deferred; launch venues+bands first; mine venue replies for tech demand signals (FR-8.1); position tech as venue upsell (installed-PA rooms, brewery series). |
| R-3 | **Therapist audience hostility** to automated outreach (post-CareDash sensitization) | M / H (for Biz A) | Provenance transparency; ultra-low volume/high personalization; instant suppression; brand = anti-platform. Watch r/therapists sentiment explicitly in opportunity scanning. |
| R-4 | **LLM reply errors** (wrong answer to a question, tone miss with a clinician) | M / M | FAQ-grounded answers only; one agentic exchange then escalate; hostile always escalates; weekly sampled QA. |
| R-5 | **Quality failure mode** (the 11x trap: volume up, relevance down, churn) | M / H | Volume hard caps; precision-biased qualifier; conversion (not send count) as the only celebrated metric. |
| R-6 | **Bandit converges on guardrail-risky copy** (clickbait subjects win replies, draw complaints) | L / M | Guardrails evaluated per variant (FR-7.3) with immediate pause; deceptive-subject validator is non-bypassable. |
| R-7 | **Source ToS/legal drift** (Google Places caching, Bandsintown licensing) | M / L | Provenance on every candidate; blocklist; prefer licensed (IOTM) and government (NPPES) sources; counsel review before scale. |
| R-8 | **Email-finder coverage is poor for these ICPs** (not in B2B graphs) | H / M | Own-website scrape is waterfall step 1 (it's also the best source); accept lower contactable-rate, oversample discovery; track contactable% per source. |
| R-9 | **Two businesses, one founder, divided attention** | H / M | This system *is* the mitigation — but M2+ exit criteria include the <4 hrs/wk human-time metric; if breached, descope to one tenant. |
| R-10 | Demand side of both marketplaces (this system recruits supply; clients/audiences are a separate problem) | H / H (business-level) | Out of scope here, but flagged: PT's moat is consumer SEO (96% of SERPs). A supply-only win is not a business win. |

## 13. Open Questions

1. **Language/runtime** — TypeScript vs Python (operator preference; decide M0).
2. **Smartlead vs Instantly** — spike both in M0/M1 week 1.
3. **Tenant brand names** — TheraDirectory/StageMatch are placeholders; needed before domain purchase (M0, calendar-critical).
4. Which 2 states / which metro launch first? (Default proposal: TX+GA; Austin.)
5. Business A pricing after "free until first client" — flat $15–25/mo? Pay-per-inquiry (Zocdoc-style $35–110/booking)? Affects the value-prop config and is itself testable via the experiment engine.
6. Should Business B charge venues at all given GigFinesse anchors them at free? (Default: no, monetize bands' membership + later premium venue tools.)
7. Calendar-booking integration (Cal.com vs Calendly) for `interested` replies that want a call.
8. When (if ever) to expose this backend as a product to other marketplace founders — it's the whitespace the research found, but it's also a distraction from two unlaunched businesses. Park until one tenant hits its 90-day validation target.

---

*All market figures and vendor claims cited in this PRD are sourced and verification-flagged in [`research/market-research-report.md`](research/market-research-report.md).*
