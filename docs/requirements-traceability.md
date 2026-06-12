# Requirements Traceability Matrix

**Maps every PRD requirement → owning component(s) (eng-spec §3.1 inventory) → design section → how it's verified.**
Companion to [`PRD.md`](../PRD.md) (what/why) and [`engineering-spec.md`](engineering-spec.md) (how). The PRD's Appendix C traces *customer needs → requirements*; this document traces *requirements → architecture*. A requirement with no spec section is a documentation bug — there are none as of spec v3.

Verification legend: **G-n** = acceptance gate n (PRD §10) · **CS** = adapter contract suite · **CI** = lint/type/property tests · **E2E** = fake-provider harness · **SLO** = monitored production objective · **DR** = docs review · **hook** = P1/P2 design hook shipped in 0.1 schema/interfaces, feature later.

## Goals & Non-Goals

| Req | Essence | Component(s) | Design | Verified |
|---|---|---|---|---|
| G1 | Brief-first, config underneath | Program synthesizer, Config loader | §8.8 | M4 exit; FR-1.2 metric |
| G2 | Pluggable edges, opinionated core | all `adapters/*` behind Protocols | PRD §5; §3.1, §20 | CS |
| G3 | Compliance by construction | Gatekeeper, validators, suppression/halt | §7, §13 | G-3/4/6–10 |
| G4 | Honest statistics built in | Stats engine | §10 | E2E, CI (property tests) |
| G5 | Agentic but governed | autonomy presets, review surfaces, ramp | §8.8.7, §8.4, §11.4 | E2E; RX SLOs |
| G6 | Cheap, inspectable, capped | spend metering, decision traces | §9.7, §5.2 | G-7; SLO |
| G7 | Dogfooded via examples | `examples/` | §16 | G-14 |
| G8 | Secure by default | envelope, injection defenses | §9.3/9.4, §15 | G-2 |
| NG1 | No non-email channels; refusal not abstinence | Config loader (schema has no channel surface; channel configs rejected at validate) | §8.8.2, D-10 | CI (validate tests) |
| NG2 | No hosted SaaS / multi-customer | scope stance | §1 | DR |
| NG3 | Not a CRM; conversion ends at webhook | Attribution | §8.9 | G-12 |
| NG4 | US CAN-SPAM v1; regime-pluggable | Compliance-regime plugins | §13.7 | hook; G-9 |
| NG5 | No ToS-prohibited scraping | ingest screen denylist | §8.1 | CI; FR-2.3 tests |
| NG6 | Own-domain or managed sending only; no shared pools | Sending adapters | §7.6 | CS; G-4 |

## Plugin Interfaces

| Req | Essence | Component(s) | Design | Verified |
|---|---|---|---|---|
| FR-I.1 | Idempotent, retried, metered, DLQ'd adapter calls | Job queue & scheduler | §6 | CI (concurrency suite) |
| FR-I.2 | `data_basis` declaration; public-data-only; rejected at registration | adapter registry (Config loader), `prospects.data_basis` | §8.1, §5.2 | CS; CI |
| FR-I.3 | FakeProvider for every interface | testing harness | §16 | CI (the harness itself) |

## 7.0 Brief & Synthesis

| Req | Essence | Component(s) | Design | Verified |
|---|---|---|---|---|
| FR-0.1 | Brief schema: audience, typed goal, restrictions, follow-up willingness | Config loader | §8.8.1 | CI (schema tests) |
| FR-0.2 | Program synthesis, constrained by construction; live probes | Program synthesizer | §8.8.1–4 | M4 exit; E2E |
| FR-0.3 | Autonomy presets + per-campaign message-review ramp | Config loader; compose stage | §8.8.7, §8.4 | E2E |
| FR-0.4 | Progressive disclosure; edit-pinning | Program synthesizer | §8.8.5 | CI |
| FR-0.5 | Goal brainstorming on research cadence | Discovery agent (`BRAINSTORM_GOALS`) | §8.7, §9.1 | hook |
| FR-0.6 | Re-synthesis on drift | Program synthesizer (revision mode) | §8.8.6 | hook |
| FR-0.7 | Sender-profile research; human-gated trust elevation | Research subsystem | §8.10 | hook |

## 7.1 Config & CLI

| Req | Essence | Component(s) | Design | Verified |
|---|---|---|---|---|
| FR-1.1 | One YAML tree, validated, atomic apply | Config loader | D-10, §8.8.1 | CI |
| FR-1.2 | init/dry-run/approve/report flows; <30 min to program | CLI over service layer | §11.3, §8.8.4 | M4 exit (outsider test) |
| FR-1.3 | Halt: human-resume-only, nothing overrides | suppression/halt set; Gatekeeper step 1; scheduler powerless | §7.1, §7.6, §6 | **G-4** |
| FR-1.4 | Forget: one-call deletion + propagation + receipt | forget subsystem | §13.3 | **G-5** |
| FR-1.5 | Secrets via env; `doctor` health surface | CLI; §15 sweeps | §17 (doctor), §15 | CI; DR |
| FR-1.6 | API surface; CLI/dashboard are clients; transcript sync | Service layer + REST API | §11.1–11.3 | CI (API tests) |

## 7.2 Discovery, Enrichment, Qualification

| Req | Essence | Component(s) | Design | Verified |
|---|---|---|---|---|
| FR-2.1 | v1 source adapters | Source adapters | §8.1 | CS |
| FR-2.2 | Signal-kind sources → timing triggers | Source adapters; discover handler | §8.1 (signals) | hook; CS |
| FR-2.3 | Hard denylist, extend-only | ingest screen | §8.1 | CI |
| FR-2.4 | Entity resolution at ingest; merge proposals | Entity resolution | §12 | **G-6** (merge races) |
| FR-2.5 | Evidence Cards: per-fact provenance + staleness | Enricher; variable resolution | §8.2, §9.6 | G-11 |
| FR-2.6 | Calibrated verification; <5% bounce per bucket | verifier waterfall; Stats (calibration) | §8.2, §8.6 | SLO; G-7 adjacent |
| FR-2.7 | Qualifier; uncertain ⇒ disqualified; sampled QA | Qualifier | §8.3 | E2E; weekly sample |
| FR-2.8 | Correction feedback loop as few-shot exemplars | LLM subsystem (designated slot) | §9.5 | hook |
| FR-2.9 | Operator events → triggered campaigns | REST API; discover stage | §11.2, §8.1 | hook (schema P0) |
| FR-2.10 | BYO import with mandatory provenance | ingest screen (`source_adapter='import'`) | §8.1 | CI |
| FR-2.11 | Tiered research notes (campaign/cohort/strategy) | Research subsystem | §8.10 | hook |

## 7.3 Composition & Message Quality

| Req | Essence | Component(s) | Design | Verified |
|---|---|---|---|---|
| FR-3.1 | LLM-generated only; no templates; evidence-cited claims | Composer | §8.4 | **G-1** |
| FR-3.1a | Typed variable registry; trust classes; envelope interpolation | variable resolution | §9.6 | CI; G-2 |
| FR-3.2 | Claims governance: denylist P0, allowlist registry P1 | validators; claim registry | §7.3, §13.5 | G-10 |
| FR-3.3 | Per-segment tone calibration | Config (cohort voice override); Stats attributes | §10.2 | E2E |
| FR-3.4 | Budget gates at queue time | Gatekeeper steps 5–6 | §7.1 | G-7 |
| FR-3.5 | ≤3 follow-ups, ≥3-day gaps, stop conditions | schema CHECK; sequencing | §5.2 (`touches`), §7.6 | CI |
| FR-3.6 | Human tasks as sequence steps | human-task subsystem | §8.13 | hook |
| FR-3.7 | Pre-dispatch suppression/frequency; no pixels; local send windows | Gatekeeper; dispatch | §7.1, §19 (clock) | G-3/6 |
| FR-3.8 | Sender-identity honesty + automation disclosure | validators | §7.3 | G-9 family |
| FR-3.9 | No bump theater — follow-up value lint | validators (pack member) | §7.3 | hook (lint P1) |
| FR-3.10 | Value artifacts: collateral + generated | Artifact service | §8.12 | hook; G-1 covers artifacts |
| FR-3.11 | PHI / sector-sensitivity screen | validators; ingest | §13.6 | hook; CI corpus |

## 7.4 Reply Handling

| Req | Essence | Component(s) | Design | Verified |
|---|---|---|---|---|
| FR-4.1 | Intent classification in envelope; hostile/low-conf escalate | Reply agent | §8.5 | G-2; E2E |
| FR-4.2 | Pre-authorized typed ReplyActions; unsubscribe in minutes | Reply agent; deterministic unsub path | §8.5, §9.2 | **G-3** |
| FR-4.3 | Objection library + approved counter-snippets | Reply-flow extensions | §8.14 | hook |
| FR-4.4 | Referral asks gated on positive signal; on-behalf-of | Reply-flow extensions | §8.14 | hook |
| FR-4.5 | No-show: one re-engagement, then declined | Reply-flow extensions; state machine | §8.14, §5.4 | hook |
| FR-4.6 | Escalation queue, digest, SLA nag | queues; dashboard | §8.5, §11.4 | RX SLO |

## 7.5 Experimentation

| Req | Essence | Component(s) | Design | Verified |
|---|---|---|---|---|
| FR-5.1 | Thompson sampling, one surface per cohort | Stats engine | §10.1 | CI (math property tests) |
| FR-5.2 | Per-variant guardrail pause (deterministic) | Stats; webhook handlers | §10.1 | E2E |
| FR-5.3 | Attribute tags + pooled model | Stats engine | §10.2 | CI |
| FR-5.4 | Agentic variant generation within registry | `VARIANT_GENERATE`; prompt classes | §9.5 | hook |
| FR-5.5 | Win/loss synthesis (P2) | `WINLOSS_SYNTH` | §9.1 | hook |
| FR-5.6 | Sentiment auto-throttle | Stats engine | §10.3 | hook; E2E |

## 7.6 Discovery Agent & Rebalancing

| Req | Essence | Component(s) | Design | Verified |
|---|---|---|---|---|
| FR-6.1 | Research-cadence proposals, budget-capped | Discovery agent | §8.7 | M4 exit |
| FR-6.2 | Autonomy: off/propose/auto-within-budget | proposal applier policy gate | §8.7, §8.8.7 | E2E |
| FR-6.3 | Lookalike prospecting (P2) | Discovery agent | parked; interfaces only | hook |
| FR-6.4 | Seasonality planning (P2) | Discovery agent | parked | hook |
| FR-6.5 | Underperformance detection + rebalancing | Rebalancer | §8.11 | hook |

## 7.7 Compliance & Contact Governance

| Req | Essence | Component(s) | Design | Verified |
|---|---|---|---|---|
| FR-7.1 | Compliance identity before any send | Config (required fields); validators | §7.3, §13.7 | G-9 |
| FR-7.2 | Alias-aware global suppression | canonicalization; suppression | §13.1, §13.2 | **G-3**, G-8 |
| FR-7.3 | Entity-level cross-campaign frequency caps | Gatekeeper step 4; entity merge math | §7.1, §12.4 | G-6 |
| FR-7.4 | Kill switches; human-resume-only | kill-switch subsystem | §13.4 | G-4 family |
| FR-7.5 | Per-entity audit export | audit_events; API | §5.3, §11.2 (`GET /v1/audit`) | CI |
| FR-7.6 | Citable ethics doc in footers | docs; validators (footer link) | OSS-3; §7.3 | DR |
| FR-7.7 | Regime plugin interface | Compliance-regime plugins | §13.7 | hook; CS |

## 7.8 Observability & Attribution

| Req | Essence | Component(s) | Design | Verified |
|---|---|---|---|---|
| FR-8.1 | Weekly digest | Observability; scheduler | §14 (digest) | SLO (punctuality) |
| FR-8.2 | End-to-end cost ledger | spend metering | §9.7 | CI |
| FR-8.3 | Signed-token closed-loop attribution | Attribution | §8.9 | **G-12** |
| FR-8.4 | OTel + SLOs + DLQ alerting | Observability; Job queue | §14, §6 | SLO |
| FR-8.5 | Per-message decision traces | decision_traces | §5.2 | CI; G-1 inputs |
| FR-8.6 | Production hallucination monitoring | Groundedness auditor; scheduler (sampled) | §7.3; §8.4 ramp hand-off | hook; SLO |

## 7.9 Operator UI

| Req | Essence | Component(s) | Design | Verified |
|---|---|---|---|---|
| FR-9.1 | Full management in browser; no privileged path | Dashboard UI; service layer | §11.4 | hook; CI (API parity tests) |
| FR-9.2 | Funnel + abandonment table | Dashboard UI | §11.4 | E2E (demo fixture) |
| FR-9.3 | Drill-down with research tiers at every level | Dashboard UI | §11.4 | E2E |
| FR-9.4 | Rebalancing console | Dashboard UI; Rebalancer | §11.4, §8.11 | hook |

## Security, Reviewer Experience, Operations

| Req | Essence | Component(s) | Design | Verified |
|---|---|---|---|---|
| S-1 | Threat model in docs | docs | §15; threat-model doc | DR |
| S-2 | Prompt-injection hardening: envelope, no free-form tools, corpus | envelope; LLM subsystem | §9.3, §9.4 | **G-2** |
| S-3 | Webhook signing enforced by interface | Inbound pipeline | §11.2, §15 | G-13 |
| S-4 | Scoped keys, never echoed | §15 key handling; doctor | §15, §17 | CI (startup sweep test) |
| S-5 | SECURITY.md + dependency audit | repo + CI | §15 | CI |
| S-6 | Operator data never trains shared models | architecture (self-host, no telemetry); doctor surfacing | §15; §17 | DR; doctor check |
| RX-1 | <30-second-decidable review surfaces, CLI + UI parity | Dashboard queues; CLI | §11.4 | E2E |
| RX-2 | Digest deep-links; queue-health alerting | Observability | §14 | SLO (queue age) |
| RX-3 | Friction-ful bulk approve | Dashboard; service layer | §11.4 | CI |
| O-1 | Hard spend caps pre-call; compliance never paused by caps | spend metering | §9.7 | **G-7**; I-11 tests |
| O-2 | DLQs with retry tooling | Job queue | §6 | CI |
| O-3 | Operable by 1–3 people | deployment design | §17 | DR; runbooks |
| O-4 | RBAC/SSO (P2) | deferred | §11.1 (OQ-6) | hook |

## Open-Source Requirements

| Req | Essence | Where | Verified |
|---|---|---|---|
| OSS-1 | Apache-2.0 | LICENSE | DR |
| OSS-2 | Responsible use enforced in code (no blast/fake-persona/bump features) | validators (FR-3.8/3.9); RESPONSIBLE_USE.md | G-9/10 family; DR |
| OSS-3 | Docs as deliverable incl. ethics statement | `docs/` | DR; quickstart timing |
| OSS-4 | Typed, CI-gated, semver, PyPI | CI pipeline (§16) | CI |
| OSS-5 | Adapters as contribution unit | CS + cookiecutter | CS |
| OSS-6 | No telemetry | architecture (nothing phones home) | code review; DR |
| OSS-7 | Security hygiene | §15 | CI |
| OSS-8 | Safety never paywalled | project policy binding license/commercial choices | DR (governance) |

## Gate ↔ Invariant Cross-Reference

Gates 1–14 (PRD §10) are the executable contract; each maps to spec invariants: G-1→I-4, G-2→I-5, G-3→I-3, G-4→I-2, G-5→I-6, G-6→I-7, G-7→I-8/I-11, G-8→I-3 (aliases), G-9/10→I-9, G-11→I-4 (staleness), G-12→attribution integrity, G-13→I-10, G-14→G7 (dogfood). Gate test files: `tests/gates/test_gate_{01..14}.py` (spec §16).
