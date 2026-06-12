# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Open Reachout: a self-hosted, config-first framework for agentic outbound email outreach with non-bypassable compliance guardrails. Two documents are load-bearing and should be consulted before non-trivial changes:

- `PRD.md` — what and why; the acceptance gates in §10 are the release contract.
- `docs/engineering-spec.md` — how; the system invariants in §2 are non-negotiable (no send without a Gatekeeper claim, suppression always wins, halt stops everything, claims must cite fresh evidence, untrusted content can't trigger out-of-policy actions, `forget` deletes PII).

## Commands

```bash
uv venv && uv pip install -e ".[dev]"        # setup (add ,postgres for psycopg)

uv run ruff check src tests                  # lint
uv run mypy                                  # strict typing, configured in pyproject
uv run pytest                                # full suite
uv run pytest tests/test_gatekeeper.py       # one file
uv run pytest tests/test_bandit.py::test_name  # one test
uv run pytest -m gates                       # acceptance-gate suite (PRD §10)

reachout validate examples                   # validate example configs
reachout dry-run examples/music-marketplace/tenant.yaml --n 3   # full pipeline, zero sends, fake LLM
reachout demo && reachout serve              # dashboard at http://127.0.0.1:8714/dashboard
```

Tests suffixed `_pg` (and anything marked `postgres`) need a live Postgres 16; they read `OR_TEST_DSN` (default `postgresql+psycopg://orx:orx@127.0.0.1/orx_test`) and skip if it's unreachable. CI runs lint, the full suite, and `-m gates` against a Postgres 16 service; `disqualifying`-marked gate failures block release and cannot be waived.

LLM-backed paths run in deterministic fake mode by default; pass `--llm gemini` (the default live backend; Anthropic is the alternative) with your own key for real synthesis.

## Architecture

Package: `src/open_reachout/`. Runtime is three containers — stateless FastAPI `api`, crash-safe `worker`, and Postgres as the single source of truth (state, job queue via `FOR UPDATE SKIP LOCKED`, counters, suppression, audit, spend ledger). The prospect pipeline runs as worker stages: discover → enrich → qualify → compose → gatekeep → deliver → classify → learn, with entity resolution at ingest so frequency caps and suppression apply to the human, not the campaign.

- `core/` — the opinionated, immutable safety core: config schemas, state machine (`lifecycle.py`, `states.py`), `gatekeeper.py` (the claim transaction: row-locked frequency caps, budget counters, suppression, content-hash binding), `suppression.py`, `forget.py`, `queue.py`, `worker.py`, `compliance/validators.py`, plugin Protocols in `interfaces.py`.
- `adapters/` — the pluggable edges: LLM backends (`gemini_backend.py`, `anthropic_backend.py`), sending (own-domain SMTP + IMAP polling), sources (NPPES), and `fakes.py` for tests/dry-run.
- `agents/` — LLM-driven steps (composer, qualifier, synthesizer, discovery). Agents are sandboxed by types: outputs are validated against closed schemas (`schemas.py`); they select from enums, they don't wield tools.
- `security/envelope.py` — untrusted-content envelope; scraped/reply content is wrapped and can never supply outbound URLs or actions. An injection corpus runs in CI.
- `stats/` — Thompson-sampling bandit with attribute priors and guardrail auto-pause.
- `api/` — FastAPI app + htmx dashboard. `db/schema.sql` is the schema (with belt-and-braces triggers backing the invariants). `cli/main.py` is the Typer entry point (`reachout`).

## Hard rules

- **Invariants are not features to negotiate.** Changes that weaken suppression, halt, validators, frequency caps, claims grounding, or the envelope will be declined regardless of usefulness. Fail closed: any error evaluating a gate or suppression check blocks the send.
- `core/` must not import from `adapters/`, `agents/`, or `cli/`.
- Only `core.lifecycle` writes prospect state; only `core.gatekeeper` constructs `ClaimedTouch`.
- Gatekeeper and counter code uses raw SQL deliberately (locking semantics must stay visible) — don't refactor it onto the ORM.
- The unsubscribe/suppression/forget paths are deterministic by construction — never put an LLM or metered call on them.
- Tests are required for behavior changes; gate tests (`-m gates`) for anything touching an invariant.
- No deliverability-evasion features (see `RESPONSIBLE_USE.md`).

## Conventions

Python 3.11+, pydantic v2, SQLAlchemy 2.0 Core, mypy `--strict`, ruff (line length 100, rules E/F/I/UP/B). The designed contribution surface is adapters: implement the Protocol from `open_reachout.core.interfaces`, declare `data_basis` honestly, and pass the adapter conformance suite.
