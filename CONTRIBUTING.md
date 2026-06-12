# Contributing

Thanks for considering it. Two documents are load-bearing before any code:
[`PRD.md`](PRD.md) (what and why; the acceptance gates in §10 are the
contract) and [`docs/engineering-spec.md`](docs/engineering-spec.md) (how;
the invariants in §2 are non-negotiable).

## The designed contribution surface: adapters

New data sources, email finders, verifiers, sending providers, and compliance
regimes are the easiest and most valuable contributions. Implement the
Protocol from `open_reachout.core.interfaces`, declare your `data_basis`
honestly, and pass the adapter conformance suite (lands in M1).

## Ground rules

- **Invariants are not features to negotiate.** PRs that weaken suppression,
  halt, validators, frequency caps, claims grounding, or the envelope will be
  declined regardless of how useful the feature is. Safety lives in the open
  core forever (PRD OSS-8) — and is never paywalled.
- No deliverability-evasion features (see RESPONSIBLE_USE.md).
- `core/` must not import from `adapters/`, `agents/`, or `cli/`.
- Only `core.lifecycle` writes prospect state; only `core.gatekeeper`
  constructs `ClaimedTouch`.

## Dev loop

```bash
uv venv && uv pip install -e ".[dev]"
uv run ruff check src tests
uv run pytest            # full suite
uv run pytest -m gates   # acceptance gates
reachout validate examples
```

Tests are required for behavior changes; gate tests (`-m gates`) are required
for anything touching an invariant.
