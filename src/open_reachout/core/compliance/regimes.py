"""Compliance-regime plugins (PRD FR-7.7/NG4, spec 13.7).

A regime contributes *additional* strictness for a jurisdiction; it can never
remove the non-bypassable core set (suppression, halt, deletion, claims lint,
CAN-SPAM completeness). Composition is additive by construction: the
effective validator pack is `core ∪ regime`, assembled here — there is no API
through which a plugin could subtract a core validator.

`us_can_spam` is the v1 regime; its substance already lives in the core pack
(address/unsubscribe/identity validators), so it contributes no extras — it
exists to make the regime explicit, recordable, and swappable per tenant.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from open_reachout.core.compliance.validators import Draft, Finding, ValidatorContext

#: A regime validator: pure function, draft + ctx in, findings out — exactly
#: the core validator contract.
RegimeValidator = Callable[[Draft, ValidatorContext], list[Finding]]


@dataclass(frozen=True)
class ComplianceRegime:
    name: str
    #: extra deterministic checks, run AFTER the core pack (additive only)
    extra_validators: tuple[RegimeValidator, ...] = ()
    #: identity fields the tenant must configure before any send
    required_identity_fields: tuple[str, ...] = ("physical_address", "unsubscribe_text")
    #: documented opt-out latency bound, seconds (core target is stricter)
    unsubscribe_latency_bound_s: int = 10 * 60
    #: human-readable deletion semantics (surfaced in docs/audit)
    deletion_semantics: str = "one-call deletion via `reachout forget` (FR-1.4)"


US_CAN_SPAM = ComplianceRegime(name="us_can_spam")

_REGISTRY: dict[str, ComplianceRegime] = {US_CAN_SPAM.name: US_CAN_SPAM}


def register_regime(regime: ComplianceRegime) -> None:
    """Entry-point registration for third-party regimes. Additive only: a
    plugin cannot replace an existing regime (and so cannot weaken one)."""
    if regime.name in _REGISTRY:
        raise ValueError(f"regime {regime.name!r} already registered; regimes are immutable")
    _REGISTRY[regime.name] = regime


def get_regime(name: str) -> ComplianceRegime:
    if name not in _REGISTRY:
        raise KeyError(f"unknown compliance regime {name!r} (registered: {sorted(_REGISTRY)})")
    return _REGISTRY[name]


def regime_findings(
    name: str, draft: Draft, ctx: ValidatorContext
) -> list[Finding]:
    """The regime's ADDITIONAL findings. Core validators run regardless and
    are invoked by the caller — composition is core ∪ regime, never regime
    instead of core."""
    findings: list[Finding] = []
    for check in get_regime(name).extra_validators:
        findings.extend(check(draft, ctx))
    return findings
