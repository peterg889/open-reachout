"""The Gatekeeper: single send path (spec section 7, invariants I-1..I-9).

M0 scope: the gate *orchestration* — ordering, fail-closed semantics, refusal
taxonomy, and the ClaimedTouch construction privilege — implemented against a
storage protocol. The Postgres claim transaction (spec 7.1) binds these same
steps to row locks in M1/M2; the ordering and semantics here are the contract
it must satisfy, and the unit tests pin them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from open_reachout.core.compliance.validators import Draft, ValidatorContext, content_hash, validate


class RefusalReason(StrEnum):
    HALTED = "halted"
    VALIDATION = "validation"
    SUPPRESSED = "suppressed"
    FREQUENCY = "frequency"
    BUDGET = "budget"
    NO_CAPACITY = "no_capacity"
    CONFIDENCE = "confidence"
    INTERNAL = "internal"  # any error during evaluation -> fail closed


class GateProfile(StrEnum):
    COLD = "cold"  # full gate set
    REPLY = "reply"  # agentic replies: skip frequency/budget volume gates only


@dataclass(frozen=True)
class Refusal:
    reason: RefusalReason
    detail: str = ""
    terminal: bool = False  # terminal refusals release the draft; others retry


_CONSTRUCTION_TOKEN = object()


@dataclass(frozen=True)
class ClaimedTouch:
    """Proof of a successful claim. Only :func:`claim` can construct one;
    `SendingProvider.send` accepts nothing else (I-1)."""

    touch_id: str
    tenant: str
    mailbox: str
    content_sha256: str
    recipient: str = ""  # the verified canonical address (own-domain SMTP To)

    def __post_init__(self) -> None:
        if getattr(_claim_guard, "token", None) is not _CONSTRUCTION_TOKEN:
            raise RuntimeError(
                "ClaimedTouch may only be constructed by gatekeeper.claim() (invariant I-1)"
            )


class _ClaimGuard:
    token: object | None = None


_claim_guard = _ClaimGuard()


@dataclass(frozen=True)
class DraftTouch:
    touch_id: str
    tenant: str
    entity_id: str
    email_canonical: str
    draft: Draft
    stored_content_hash: str
    groundedness_passed_hash: str | None
    profile: GateProfile
    validator_ctx: ValidatorContext
    cohort_id: str = ""  # budget scope (I-8); unused by REPLY profile


class GateStore(Protocol):
    """Storage the gates consult. The Postgres implementation evaluates these
    inside one transaction with row locks (spec 7.1); fakes back the tests."""

    def halted_scopes(self, tenant: str) -> list[str]: ...
    def is_suppressed(self, email_canonical: str, tenant: str) -> bool: ...
    def frequency_ok(self, entity_id: str, tenant: str) -> bool: ...
    def try_consume_budget(self, tenant: str, touch: DraftTouch) -> bool: ...
    def release_budget(self, tenant: str, touch: DraftTouch) -> None: ...
    def pick_mailbox(self, tenant: str) -> str | None: ...
    def confidence_sendable(self, email_canonical: str, tenant: str) -> bool: ...
    def persist_claim(
        self, touch: DraftTouch, mailbox: str, gate_results: dict[str, str]
    ) -> None: ...


def claim(store: GateStore, touch: DraftTouch) -> ClaimedTouch | Refusal:
    """Evaluate all gates in pinned order; fail closed on any error.

    Order is part of the contract (cheap/absolute gates first; gates with
    side effects last so refusals never leak partial state).
    """
    results: dict[str, str] = {}
    try:
        # 1. halt / kill switches (I-2) — absolute, both profiles
        if store.halted_scopes(touch.tenant):
            return _refuse(results, "halt", RefusalReason.HALTED, terminal=False)
        results["halt"] = "pass"

        # 2. validators re-run + hash binding (I-9) — both profiles
        if content_hash(touch.draft) != touch.stored_content_hash:
            return _refuse(results, "content_hash", RefusalReason.VALIDATION, terminal=True)
        findings = validate(touch.draft, touch.validator_ctx)
        if findings:
            return _refuse(
                results,
                "validators",
                RefusalReason.VALIDATION,
                terminal=True,
                detail="; ".join(f"{f.code}:{f.detail}" for f in findings),
            )
        if touch.groundedness_passed_hash != touch.stored_content_hash:
            return _refuse(results, "groundedness", RefusalReason.VALIDATION, terminal=True)
        results["validators"] = "pass"

        # 3. suppression (I-3) — absolute, both profiles
        if store.is_suppressed(touch.email_canonical, touch.tenant):
            return _refuse(results, "suppression", RefusalReason.SUPPRESSED, terminal=True)
        results["suppression"] = "pass"

        if touch.profile is GateProfile.COLD:
            # 4. entity frequency (I-7)
            if not store.frequency_ok(touch.entity_id, touch.tenant):
                return _refuse(results, "frequency", RefusalReason.FREQUENCY, terminal=True)
            results["frequency"] = "pass"

            # 5. volume budgets (I-8) — consumes; everything after must release on failure
            if not store.try_consume_budget(touch.tenant, touch):
                return _refuse(results, "budget", RefusalReason.BUDGET, terminal=False)
            results["budget"] = "pass"

        # 6. mailbox capacity
        mailbox = store.pick_mailbox(touch.tenant)
        if mailbox is None:
            if touch.profile is GateProfile.COLD:
                store.release_budget(touch.tenant, touch)
            return _refuse(results, "mailbox", RefusalReason.NO_CAPACITY, terminal=False)
        results["mailbox"] = mailbox

        # 7. verification confidence (FR-2.6)
        if not store.confidence_sendable(touch.email_canonical, touch.tenant):
            if touch.profile is GateProfile.COLD:
                store.release_budget(touch.tenant, touch)
            return _refuse(results, "confidence", RefusalReason.CONFIDENCE, terminal=True)
        results["confidence"] = "pass"

        # 8. persist
        store.persist_claim(touch, mailbox, results)
        _claim_guard.token = _CONSTRUCTION_TOKEN
        try:
            return ClaimedTouch(
                touch_id=touch.touch_id,
                tenant=touch.tenant,
                mailbox=mailbox,
                content_sha256=touch.stored_content_hash,
                recipient=touch.email_canonical,
            )
        finally:
            _claim_guard.token = None
    except Exception as exc:  # noqa: BLE001 — fail closed is the point (I-x)
        return Refusal(RefusalReason.INTERNAL, detail=repr(exc), terminal=False)


def _refuse(
    results: dict[str, str],
    gate: str,
    reason: RefusalReason,
    *,
    terminal: bool,
    detail: str = "",
) -> Refusal:
    results[gate] = f"refused:{reason}"
    return Refusal(reason, detail=detail or gate, terminal=terminal)


@dataclass(frozen=True)
class ClaimedSnapshot:
    """What a DispatchStore returns for a touch that is verifiably claimed
    and still sendable (halt and suppression re-checked at load time)."""

    touch_id: str
    tenant: str
    mailbox: str
    content_sha256: str
    recipient: str = ""


class DispatchStore(Protocol):
    def load_claimed(self, touch_id: str) -> ClaimedSnapshot | None: ...


def reissue(store: DispatchStore, touch_id: str) -> ClaimedTouch | Refusal:
    """Reconstruct a ClaimedTouch for dispatch retries (spec 7.4).

    Construction stays inside this module (I-1): the store may only return a
    snapshot for a touch whose DB status is 'claimed' AND that passes the
    absolute gates (halt, suppression) again at load time — reactive
    enforcement between claim and dispatch.
    """
    try:
        snapshot = store.load_claimed(touch_id)
    except Exception as exc:  # noqa: BLE001 — fail closed
        return Refusal(RefusalReason.INTERNAL, detail=repr(exc), terminal=False)
    if snapshot is None:
        return Refusal(RefusalReason.VALIDATION, detail="not claimed/sendable", terminal=True)
    _claim_guard.token = _CONSTRUCTION_TOKEN
    try:
        return ClaimedTouch(
            touch_id=snapshot.touch_id,
            tenant=snapshot.tenant,
            mailbox=snapshot.mailbox,
            content_sha256=snapshot.content_sha256,
            recipient=snapshot.recipient,
        )
    finally:
        _claim_guard.token = None
