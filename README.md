# Open Reachout

**A free, open-source framework for agentic outbound outreach.** Declare personas in config; the framework discovers matching prospects on the open web (public registries, Google Places, directories, agentic search — not B2B contact databases), enriches and qualifies them from their own web presence, sends personalized cold email with bounded drip follow-ups through your own sending-provider account, handles replies agentically, optimizes messaging with honest bandit experiments, and proposes new cohorts from outcome data.

Self-hosted. BYO API keys. Compliance guardrails (CAN-SPAM, volume caps, suppression-first sending) live in the core and are non-bypassable.

## Why

- Commercial "AI SDR" tools ($250–$5,000/mo) are closed boxes welded to B2B contact databases and B2B SaaS sales motions. None handles prospects who live on government registries, Google Maps, or their own websites; none does outcome-driven cohort discovery; none exposes a verifiable experimentation loop.
- The open-source shelf is empty: only toy/demo projects exist. No production-grade OSS framework with a real pipeline, sending integration, low-volume statistics, or a compliance posture.
- The hard-won knowledge (deliverability limits, warmup, CAN-SPAM completeness, why bandits beat A/B significance theater at cold-email volumes) is encodable — so builders stop re-learning it by burning domains.

## Status

**Sends from your own domain.** `reachout run --provider smtp` sends each
gatekeeper-claimed touch directly from your own authenticated mailbox
(Google Workspace / Microsoft 365 / self-hosted, via `OR_SMTP_MAILBOXES`) —
your domain and reputation, with From, Message-ID, and one-click unsubscribe
all on your domain. Because the framework drives each send itself, every gate
(suppression, halt, frequency, budget) is enforced in the same transaction as
the SMTP socket — no reactive-pause gap. Inbound replies and bounces correlate
back by Message-ID via IMAP polling — `reachout poll` fetches your
inboxes and feeds the same event pipeline as provider webhooks, and
`reachout doctor` verifies your domain's SPF/DMARC/DKIM/MX against the
Google/Microsoft bulk-sender floor before you send. (Managed providers
like Smartlead remain an option for bundled warmup/rotation.)

**Presentable dashboard.** `reachout demo && reachout serve` then open
`http://127.0.0.1:8714/dashboard`: top-level metrics (reached, replies,
positive responses, conversions) with a funnel and an explicit
*abandonment* table showing where the flow loses people; cohorts the
system is working, each drilling into the **strategies being tested**
(bandit arms with live/paused status and per-strategy research notes)
and the **members** of the cohort; each member drills into their
**background research** (Evidence Card with provenance and observation
dates) and the full **conversation history** (sent drafts and replies,
threaded). Research happens at every level of granularity — cohort notes
inform segments, strategy notes inform prompts, evidence cards inform
each email — refreshed by `reachout research` (data-only, or with an LLM
narrative via `--llm gemini`).

**M0–M4 core complete.** `reachout init --from-brief brief.yaml --tenant x`
now compiles a Brief into a full program (personas, cohorts, generation
prompts) with provenance and a Program Proposal containing sample emails —
deterministic scaffold in fake mode, real synthesis with `--llm gemini`.
The schemas are the enforcement layer: synthesis cannot exceed budgets,
raise follow-up caps, or reference unregistered variable slots. The discovery agent closes the loop: `reachout discover`
mines per-cohort outcomes and files Proposals (shift budget from a
losing cohort to a winning one, flag a zero-conversion cohort), which a
human approves or declines via `reachout approve` — declines are
remembered for 90 days, and approving a budget shift moves the live
cohort caps. The learning loop now runs end-to-end:
dispatches record bandit trials, interested replies record successes,
bounces/complaints feed deterministic guardrail auto-pause, hostile or
uncertain replies land in a real escalation queue (`reachout approve`),
and `reachout report` prints the operator digest (funnel, budgets,
replies, variant leaderboard, compliance, queue health).
**Event-triggered campaigns (FR-2.9) are live**: a cohort declared with
`trigger: { event_type: compact.issuing }` stays dormant until the
operator's own systems fire `POST /v1/events` — then matching qualified
prospects start sequences and a selector-narrowed discovery pass runs,
all through the full gate set (the therapist example ships a
`compact_newly_eligible` cohort wired this way). Beneath that,
the live-path safety core runs against real Postgres: the gatekeeper claim transaction (row-locked frequency
caps, guarded budget counters with audited compensation, mailbox capacity,
hash binding), alias+tombstone-aware suppression, `reachout halt`/`resume`
(human-resume-only) and `reachout forget` (one-call deletion with receipts
and provider-propagation jobs), the job queue (lease/retry/DLQ/reaper), and
the reply pipeline whose unsubscribe path is deterministic — opt-outs never
wait on a model. CI runs the whole suite, including the disqualifying gates,
against a Postgres 16 service.

Earlier milestones: Implemented and tested: the pure-logic core
(canonicalization, state machine, config schemas, trust-classed variable
registry, compliance validators, untrusted-content envelope, gatekeeper claim
orchestration, job-queue SQL, schema with belt-and-braces triggers), plus the
M1 pipeline: Thompson-sampling bandit with attribute priors and guardrails,
the composer (prompt + variables → validated, groundedness-audited draft with
retry-and-escalate), the qualifier, evidence staleness rules, the NPPES
source adapter, LLM backends (**Gemini is the default live backend**;
Anthropic available; both BYO-key, structured-output based), the injection
corpus running in CI, and a working `reachout dry-run` — the full pipeline
through compose with zero sends.

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest && uv run pytest -m gates
reachout validate examples
reachout dry-run examples/music-marketplace/tenant.yaml --n 3   # fake LLM
# with a key: pip install -e ".[gemini]" && reachout dry-run ... --llm gemini
```

| Doc | Contents |
|---|---|
| [`PRD.md`](PRD.md) | Full framework requirements: plugin interfaces, pipeline, domain model, compliance core, experimentation engine, discovery agent, OSS requirements (license, responsible use, docs), acceptance gates, milestones, risks, customer-needs traceability |
| [`docs/engineering-spec.md`](docs/engineering-spec.md) | System architecture & engineering spec: system invariants and their enforcement mechanisms, component inventory, schema, job system, the Gatekeeper send path, LLM/injection subsystem, stats engine, API surface + dashboard, compliance subsystems, testing/gate suite, ops, failure-mode analysis |
| [`docs/requirements-traceability.md`](docs/requirements-traceability.md) | Every PRD requirement mapped to its owning component, engineering-spec design section, and verification method (gate / contract suite / CI / SLO) |
| [`research/market-research-report.md`](research/market-research-report.md) | The deep-research report behind the PRD: competitive landscape, compliance/deliverability constraints, build-vs-buy stack, benchmarks — multi-source, adversarially verified, fully cited |

The full prospecting pipeline runs through the worker against Postgres:
`discover -> enrich -> qualify -> compose -> deliver`, with **entity
resolution at ingest** — a venue owner who also gigs (same email across two
personas) resolves to one entity, so the cross-campaign frequency cap and
suppression apply to the human, not the campaign. Ingest screening drops
denylisted sources (Psychology Today), suppressed, and forgotten candidates
before they ever become prospects. The live source/enricher adapters
(Google Places, Firecrawl) are the only account-bound gap; everything else
runs today on fakes.

## Reference use cases (`examples/`, planned)

The framework is dogfooded by two real marketplace businesses whose supply acquisition runs on it:

1. **`therapist-directory`** — recruiting individual therapists in private practice for a Psychology Today–competitor directory (NPPES registry sourcing, trust-first voice, the "CareDash rule": registry data for private outreach only, never public profile pre-population).
2. **`music-marketplace`** — recruiting small venues, bands, and (later) sound techs for a three-sided membership marketplace (Google Places discovery, licensed venue databases, multi-persona tenancy, demand-signal mining).

Releases are gated on both examples working — the abstractions stay honest because the author's own businesses depend on them.

## Design commitments

- **Config-first:** a whole outreach program is versionable YAML; `reachout validate && reachout run` is the interface.
- **Pluggable edges, opinionated core:** sources, enrichers, email finders, verifiers, senders, LLMs, and reply actions are entry-point plugins; budgets, suppression, sequencing caps, and compliance validators are core and immutable.
- **Own-domain sending, no shipped data, no telemetry.** Send from your own authenticated domain (direct SMTP) or a managed cold-email provider — never a shared transactional ESP pool; the project ships adapters, never contact data; your outreach data never leaves your deployment.
- **Honest statistics:** Thompson-sampling bandits with guardrail pausing and pooled attribute learning — because detecting a 5%→7% reply lift classically needs ~2,200 recipients per arm, which cold-email volumes never reach.

License: Apache-2.0 (planned).
