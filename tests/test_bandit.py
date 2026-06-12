import random

import pytest

from open_reachout.stats.bandit import (
    VariantArm,
    guardrail_breaches,
    pooled_rate,
    prior_for,
    select,
)


def test_thompson_prefers_the_better_arm() -> None:
    rng = random.Random(7)
    good = VariantArm("good", trials=200, successes=30)
    bad = VariantArm("bad", trials=200, successes=4)
    picks = [select([good, bad], {}, 0.06, rng).variant_id for _ in range(300)]
    assert picks.count("good") > 270


def test_paused_arms_are_never_selected_and_empty_fails_closed() -> None:
    rng = random.Random(1)
    paused = VariantArm("p", paused=True)
    live = VariantArm("l")
    assert select([paused, live], {}, 0.06, rng).variant_id == "l"
    with pytest.raises(ValueError, match="no live variants"):
        select([paused], {}, 0.06, rng)


def test_attribute_prior_pulls_cold_start() -> None:
    """A brand-new variant with historically strong attributes should win the
    sampling more often than one with weak attributes (spec 10.2)."""
    rng = random.Random(3)
    effects = {("tone", "warm"): 0.12, ("tone", "formal"): 0.02}
    warm = VariantArm("warm", attributes={"tone": "warm"})
    formal = VariantArm("formal", attributes={"tone": "formal"})
    picks = [select([warm, formal], effects, 0.06, rng).variant_id for _ in range(400)]
    assert picks.count("warm") > 280


def test_pooled_rate_shrinks_toward_global() -> None:
    # 3/10 observed, global 5%: pooled estimate sits between, nearer global.
    rate = pooled_rate(3, 10, 0.05)
    assert 0.05 < rate < 0.30
    assert abs(rate - 0.05) < abs(rate - 0.30)
    assert prior_for({}, {}, 0.06) == 0.06


def test_guardrails_pause_regardless_of_reply_performance() -> None:
    hot = VariantArm("hot", trials=50, successes=20, complaints=1)  # 2% complaints
    assert "complaint_rate" in guardrail_breaches(hot)
    young = VariantArm("young", trials=5, successes=0, complaints=1)
    assert guardrail_breaches(young) == []  # below MIN_TRIALS: no verdict yet
