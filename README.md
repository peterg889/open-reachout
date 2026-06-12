# Open Reachout

**A free, open-source framework for agentic outbound outreach.** Declare personas in config; the framework discovers matching prospects on the open web (public registries, Google Places, directories, agentic search — not B2B contact databases), enriches and qualifies them from their own web presence, sends personalized cold email with bounded drip follow-ups through your own sending-provider account, handles replies agentically, optimizes messaging with honest bandit experiments, and proposes new cohorts from outcome data.

Self-hosted. BYO API keys. Compliance guardrails (CAN-SPAM, volume caps, suppression-first sending) live in the core and are non-bypassable.

## Why

- Commercial "AI SDR" tools ($250–$5,000/mo) are closed boxes welded to B2B contact databases and B2B SaaS sales motions. None handles prospects who live on government registries, Google Maps, or their own websites; none does outcome-driven cohort discovery; none exposes a verifiable experimentation loop.
- The open-source shelf is empty: only toy/demo projects exist. No production-grade OSS framework with a real pipeline, sending integration, low-volume statistics, or a compliance posture.
- The hard-won knowledge (deliverability limits, warmup, CAN-SPAM completeness, why bandits beat A/B significance theater at cold-email volumes) is encodable — so builders stop re-learning it by burning domains.

## Status

Pre-code. Currently in research/specification phase.

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
