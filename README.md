# Open Reachout

A unified, config-driven agentic outreach backend for acquiring marketplace supply: define personas, set a monthly volume, and the system discovers prospects from public/vertical data sources, sends personalized cold email with bounded drip follow-ups, handles replies agentically, optimizes messaging with bandit experiments, and proposes new cohorts from outcome data.

Built to serve two businesses from one codebase:

- **Business A** — a Psychology Today–competitor therapist directory (supply = individual therapists in private practice).
- **Business B** — a three-sided membership marketplace for bands, sound techs, and small venues (cafes, bars, breweries, wineries).

## Documents

| Doc | Contents |
|---|---|
| [`PRD.md`](PRD.md) | Full product requirements: vision, domain model, core loop, functional requirements, architecture, tenant configs, milestones, risks, open questions |
| [`research/market-research-report.md`](research/market-research-report.md) | Deep-research report behind the PRD: competitive landscape (AI SDRs, therapist directories, gig marketplaces), compliance/deliverability constraints, build-vs-buy stack, benchmarks — multi-source, adversarially verified, fully cited |

## Headline research findings (June 2026)

1. Autonomous persona-based outreach exists for B2B SaaS sales ($250–$5,000/mo), but **no product does outcome-driven cohort discovery, none exposes an honest experimentation loop, and none targets marketplace supply acquisition** — therapists, bands, and cafes aren't in the B2B contact databases all incumbents depend on.
2. Buy the plumbing (~$430–500/mo all-in for both tenants), build the brain (discovery, qualification, composition, reply handling, bandit experimentation, cohort discovery).
3. Compliance and deliverability constraints are encodable and non-negotiable: CAN-SPAM completeness, ≤25 sends/inbox/day, <0.1% complaint rate, suppression-first, no ToS-prohibited scraping (notably Psychology Today), and never pre-populating public profiles from registry data (the CareDash rule).
