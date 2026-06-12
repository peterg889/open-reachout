"""Gatekeeper contract tests: ordering, fail-closed, construction privilege.
The Postgres claim transaction (M2) must satisfy exactly these semantics."""

from __future__ import annotations

import pytest

from open_reachout.core.compliance.validators import Draft, ValidatorContext, content_hash
from open_reachout.core.gatekeeper import (
    ClaimedTouch,
    DraftTouch,
    GateProfile,
    Refusal,
    RefusalReason,
    claim,
)

CTX = ValidatorContext(
    physical_address="1 Main St, Austin TX",
    unsubscribe_text="unsubscribe here",
    sender_identity="Maya Reyes, StageMatch",
    allowed_url_prefixes=("https://ok.test/",),
)

GOOD_BODY = (
    "Hi - quick note about your venue. "
    "- Maya Reyes, StageMatch\n1 Main St, Austin TX\nunsubscribe here"
)


class FakeStore:
    def __init__(self, **overrides: object) -> None:
        self.halted: list[str] = []
        self.suppressed: set[str] = set()
        self.frequency_pass = True
        self.budget_available = 1
        self.released = 0
        self.mailbox: str | None = "mb-1@send.test"
        self.confidence_pass = True
        self.persisted: list[str] = []
        for k, v in overrides.items():
            setattr(self, k, v)

    def halted_scopes(self, tenant: str) -> list[str]:
        return self.halted

    def is_suppressed(self, email_canonical: str, tenant: str) -> bool:
        return email_canonical in self.suppressed

    def frequency_ok(self, entity_id: str, tenant: str) -> bool:
        return self.frequency_pass

    def sequence_continuation_ok(self, entity_id: str, prospect_id: str) -> bool:
        return getattr(self, "continuation_pass", True)

    def try_consume_budget(self, tenant: str, touch: DraftTouch) -> bool:
        if self.budget_available > 0:
            self.budget_available -= 1
            return True
        return False

    def release_budget(self, tenant: str, touch: DraftTouch) -> None:
        self.released += 1
        self.budget_available += 1

    def pick_mailbox(self, tenant: str) -> str | None:
        return self.mailbox

    def confidence_sendable(self, email_canonical: str, tenant: str) -> bool:
        return self.confidence_pass

    def persist_claim(self, touch: DraftTouch, mailbox: str, gate_results: dict[str, str]) -> None:
        self.persisted.append(touch.touch_id)


def make_touch(profile: GateProfile = GateProfile.COLD, body: str = GOOD_BODY) -> DraftTouch:
    draft = Draft(subject="About Thursday nights", body=body)
    digest = content_hash(draft)
    return DraftTouch(
        touch_id="t-1",
        tenant="stagematch",
        entity_id="e-1",
        email_canonical="owner@venue.test",
        draft=draft,
        stored_content_hash=digest,
        groundedness_passed_hash=digest,
        profile=profile,
        validator_ctx=CTX,
    )


def test_happy_path_claims_and_persists() -> None:
    store = FakeStore()
    result = claim(store, make_touch())
    assert isinstance(result, ClaimedTouch)
    assert store.persisted == ["t-1"]


@pytest.mark.gates
@pytest.mark.disqualifying
def test_gate04_halt_blocks_everything() -> None:
    """Gate 4: during a halt no profile can claim — including replies."""
    for profile in GateProfile:
        store = FakeStore(halted=["global"])
        result = claim(store, make_touch(profile))
        assert isinstance(result, Refusal) and result.reason is RefusalReason.HALTED
        assert store.persisted == []


@pytest.mark.gates
@pytest.mark.disqualifying
def test_gate03_suppression_blocks_everything() -> None:
    for profile in GateProfile:
        store = FakeStore(suppressed={"owner@venue.test"})
        result = claim(store, make_touch(profile))
        assert isinstance(result, Refusal) and result.reason is RefusalReason.SUPPRESSED


@pytest.mark.gates
@pytest.mark.disqualifying
def test_gate01_groundedness_hash_required() -> None:
    """Gate 1 plumbing: a draft without a matching groundedness stamp can't claim."""
    touch = make_touch()
    tampered = DraftTouch(**{**touch.__dict__, "groundedness_passed_hash": None})
    result = claim(FakeStore(), tampered)
    assert isinstance(result, Refusal) and result.reason is RefusalReason.VALIDATION


def test_validate_then_bind_rejects_swapped_content() -> None:
    touch = make_touch()
    swapped = DraftTouch(
        **{
            **touch.__dict__,
            "draft": Draft(subject="About Thursday nights", body=GOOD_BODY + " EXTRA"),
        }
    )
    result = claim(FakeStore(), swapped)
    assert isinstance(result, Refusal) and result.reason is RefusalReason.VALIDATION


def test_budget_refusal_is_retryable_and_frequency_terminal() -> None:
    over_budget = claim(FakeStore(budget_available=0), make_touch())
    assert isinstance(over_budget, Refusal)
    assert over_budget.reason is RefusalReason.BUDGET and not over_budget.terminal

    frequency = claim(FakeStore(frequency_pass=False), make_touch())
    assert isinstance(frequency, Refusal)
    assert frequency.reason is RefusalReason.FREQUENCY and frequency.terminal


def test_no_capacity_releases_consumed_budget() -> None:
    store = FakeStore(mailbox=None)
    result = claim(store, make_touch())
    assert isinstance(result, Refusal) and result.reason is RefusalReason.NO_CAPACITY
    assert store.released == 1 and store.budget_available == 1


def test_reply_profile_skips_volume_gates_only() -> None:
    store = FakeStore(budget_available=0, frequency_pass=False)
    result = claim(store, make_touch(GateProfile.REPLY))
    assert isinstance(result, ClaimedTouch)


def test_fail_closed_on_store_errors() -> None:
    class ExplodingStore(FakeStore):
        def is_suppressed(self, email_canonical: str, tenant: str) -> bool:
            raise RuntimeError("db gone")

    result = claim(ExplodingStore(), make_touch())
    assert isinstance(result, Refusal) and result.reason is RefusalReason.INTERNAL


def test_claimedtouch_constructor_is_private() -> None:
    """Invariant I-1: nothing outside claim() can mint a ClaimedTouch."""
    with pytest.raises(RuntimeError, match="I-1"):
        ClaimedTouch(touch_id="x", tenant="t", mailbox="m", content_sha256="h")
