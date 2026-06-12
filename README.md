# Open Reachout

**A free, open-source framework for agentic outbound outreach.** Declare personas in config; the framework discovers matching prospects on the open web (public registries, Google Places, directories, agentic search — not B2B contact databases), enriches and qualifies them from their own web presence, sends personalized cold email with bounded drip follow-ups through your own sending-provider account, handles replies agentically, optimizes messaging with honest bandit experiments, and proposes new cohorts from outcome data.

Self-hosted. BYO API keys. Compliance guardrails (CAN-SPAM, volume caps, suppression-first sending) live in the core and are non-bypassable.

## Why

- Commercial "AI SDR" tools ($250–$5,000/mo) are closed boxes welded to B2B contact databases and B2B SaaS sales motions. None handles prospects who live on government registries, Google Maps, or their own websites; none does outcome-driven cohort discovery; none exposes a verifiable experimentation loop.
- The open-source shelf is empty: only toy/demo projects exist. No production-grade OSS framework with a real pipeline, sending integration, low-volume statistics, or a compliance posture.
- The hard-won knowledge (deliverability limits, warmup, CAN-SPAM completeness, why bandits beat A/B significance theater at cold-email volumes) is encodable — so builders stop re-learning it by burning domains.

## Status

**M0–M4 core complete.** `reachout init --from-brief brief.yaml --tenant x`
now compiles a Brief into a full program (personas, cohorts, generation
prompts) with provenance and a Program Proposal containing sample emails —
deterministic scaffold in fake mode, real synthesis with `--llm gemini`.
The schemas are the enforcement layer: synthesis cannot exceed budgets,
raise follow-up caps, or reference unregistered variable slots. The learning loop now runs end-to-end:
dispatches record bandit trials, interested replies record successes,
bounces/complaints feed deterministic guardrail auto-pause, hostile or
uncertain replies land in a real escalation queue (`reachout approve`),
and `reachout report` prints the operator digest (funnel, budgets,
replies, variant leaderboard, compliance, queue health). Beneath that,
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
| [`docs/engineering-spec.md`](docs/engineering-spec.md) | System architecture & engineering spec: system invariants and their enforcement mechanisms, schema, job system, the Gatekeeper send path, LLM/injection subsystem, stats engine, API surface, compliance subsystems, testing/gate suite, ops, failure-mode analysis |
| [`research/market-research-report.md`](research/market-research-report.md) | The deep-research report behind the PRD: competitive landscape, compliance/deliverability constraints, build-vs-buy stack, benchmarks — multi-source, adversarially verified, fully cited |

## Reference use cases (`examples/`, planned)

The framework is dogfooded by two real marketplace businesses whose supply acquisition runs on it:

1. **`therapist-directory`** — recruiting individual therapists in private practice for a Psychology Today–competitor directory (NPPES registry sourcing, trust-first voice, the "CareDash rule": registry data for private outreach only, never public profile pre-population).
2. **`music-marketplace`** — recruiting small venues, bands, and (later) sound techs for a three-sided membership marketplace (Google Places discovery, licensed venue databases, multi-persona tenancy, demand-signal mining).

Releases are gated on both examples working — the abstractions stay honest because the author's own businesses depend on them.

## Design commitments

- **Config-first:** a whole outreach program is versionable YAML; `reachout validate && reachout run` is the interface.
- **Pluggable edges, opinionated core:** sources, enrichers, email finders, verifiers, senders, LLMs, and reply actions are entry-point plugins; budgets, suppression, sequencing caps, and compliance validators are core and immutable.
- **No SMTP, no shipped data, no telemetry.** Sending is delegated to cold-email providers (their abuse incentives stay in the loop); the project ships adapters, never contact data; your outreach data never leaves your deployment.
- **Honest statistics:** Thompson-sampling bandits with guardrail pausing and pooled attribute learning — because detecting a 5%→7% reply lift classically needs ~2,200 recipients per arm, which cold-email volumes never reach.

License: Apache-2.0 (planned).
