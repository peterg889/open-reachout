"""Thompson-sampling bandit with attribute-informed priors (spec section 10.1-10.2).

Pure math, no storage: callers load `VariantArm`s and persist updated counts.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

#: Prior strength: how many pseudo-observations the attribute model contributes.
KAPPA = 20.0
#: Shrinkage weight for attribute pooling (spec 10.2).
TAU = 50.0

#: Guardrail thresholds (FR-5.2): breach with >= MIN_TRIALS pauses the variant.
GUARDRAIL_MIN_TRIALS = 10
GUARDRAIL_LIMITS = {"complaint_rate": 0.002, "unsub_rate": 0.02, "bounce_rate": 0.05}


@dataclass
class VariantArm:
    variant_id: str
    trials: int = 0
    successes: int = 0
    attributes: dict[str, str] = field(default_factory=dict)
    paused: bool = False
    # guardrail counters
    complaints: int = 0
    unsubs: int = 0
    bounces: int = 0

    def posterior(self, prior_p: float) -> tuple[float, float]:
        alpha0 = KAPPA * prior_p
        beta0 = KAPPA * (1.0 - prior_p)
        return alpha0 + self.successes, beta0 + (self.trials - self.successes)


def pooled_rate(observed_s: int, observed_n: int, global_rate: float, tau: float = TAU) -> float:
    """Empirical-Bayes shrinkage of an attribute-value rate toward the global rate."""
    return (observed_s + tau * global_rate) / (observed_n + tau)


def prior_for(
    attributes: dict[str, str],
    attribute_effects: dict[tuple[str, str], float],
    global_rate: float,
) -> float:
    """A variant's prior success rate = mean of its attributes' pooled effects,
    falling back to the global rate for unseen attributes."""
    rates = [
        attribute_effects.get((name, value), global_rate)
        for name, value in sorted(attributes.items())
    ]
    return sum(rates) / len(rates) if rates else global_rate


def select(
    arms: list[VariantArm],
    attribute_effects: dict[tuple[str, str], float],
    global_rate: float,
    rng: random.Random,
) -> VariantArm:
    """Thompson sampling: sample each live arm's posterior, take the argmax.

    Fails closed: no live arms is an error the caller must surface, not paper over.
    """
    live = [a for a in arms if not a.paused]
    if not live:
        raise ValueError("no live variants to select from")
    best: tuple[float, VariantArm] | None = None
    for arm in live:
        alpha, beta = arm.posterior(prior_for(arm.attributes, attribute_effects, global_rate))
        sample = rng.betavariate(alpha, beta)
        if best is None or sample > best[0]:
            best = (sample, arm)
    assert best is not None
    return best[1]


def guardrail_breaches(arm: VariantArm) -> list[str]:
    """Names of breached guardrails (FR-5.2). Deterministic; runs in webhook handlers."""
    if arm.trials < GUARDRAIL_MIN_TRIALS:
        return []
    rates = {
        "complaint_rate": arm.complaints / arm.trials,
        "unsub_rate": arm.unsubs / arm.trials,
        "bounce_rate": arm.bounces / arm.trials,
    }
    return [name for name, rate in rates.items() if rate > GUARDRAIL_LIMITS[name]]
