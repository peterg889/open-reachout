"""Deterministic message validators (PRD FR-3.1/3.2/3.8/3.9, invariant I-9).

These run twice: at compose time for fast feedback and again inside the
gatekeeper claim against the stored content hash. They are pure functions:
draft + context in, violations out. An empty violation list is the only pass.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum


class Violation(StrEnum):
    MISSING_PHYSICAL_ADDRESS = "missing_physical_address"
    MISSING_UNSUBSCRIBE = "missing_unsubscribe"
    FAKE_THREAD_SUBJECT = "fake_thread_subject"
    DECEPTIVE_SUBJECT = "deceptive_subject"
    TOO_LONG = "too_long"
    FORBIDDEN_CLAIM = "forbidden_claim"
    UNREGISTERED_CLAIM = "unregistered_claim"
    PHI_SUSPECTED = "phi_suspected"
    URL_NOT_ALLOWLISTED = "url_not_allowlisted"
    BUMP_THEATER = "bump_theater"
    MISSING_SENDER_IDENTITY = "missing_sender_identity"
    EMPTY = "empty"


@dataclass(frozen=True)
class Finding:
    code: Violation
    detail: str


@dataclass(frozen=True)
class Draft:
    subject: str
    body: str
    step_index: int = 0  # 0 = opener, >=1 follow-ups


@dataclass(frozen=True)
class ValidatorContext:
    physical_address: str
    unsubscribe_text: str
    sender_identity: str  # real person or honestly-branded team (FR-3.8)
    allowed_url_prefixes: tuple[str, ...]
    max_words: int = 200
    forbidden_patterns: tuple[str, ...] = ()  # tenant extensions to the default pack
    # FR-3.2 allowlist mode: claim-like sentences must contain an approved phrase.
    claim_mode: str = "denylist"  # denylist | allowlist
    approved_claims: tuple[str, ...] = ()
    claim_registry_version: str = "deny-pack@1"  # recorded per send (FR-8.5)
    sector_sensitivity: str = "none"  # none | healthcare (FR-3.11 PHI screen)
    compliance_regime: str = "us_can_spam"  # FR-7.7; extras run additively


# Default forbidden-claims pack (FR-3.2 denylist): ROI/earnings promises,
# implied relationships. Tenants extend, never shrink.
DEFAULT_FORBIDDEN: tuple[str, ...] = (
    r"\bguaranteed?\b.{0,40}\b(clients?|results?|bookings?|income|revenue)\b",
    r"\byou(?:'| wi)ll (?:get|gain|earn)\b.{0,30}\b\d+",
    r"\bas (?:we|i) discussed\b",
    r"\bper our (?:conversation|call|chat)\b",
    r"\bfollowing up on our\b",
    r"\brisk[- ]free\b",
)

_FAKE_THREAD = re.compile(r"^\s*(re|fwd?)\s*:", re.IGNORECASE)
_DECEPTIVE = re.compile(r"(urgent|act now|final notice|account (?:suspended|alert))", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s>\")\]]+", re.IGNORECASE)

_BUMP_PATTERNS = re.compile(
    r"(just (?:bumping|floating|checking in|circling back)|"
    r"bumping this (?:up|to the top)|any thoughts\s*\?\s*$)",
    re.IGNORECASE,
)

#: Sentences that assert something about the operator's own product/offer
#: (FR-3.2 allowlist mode): pricing, percentages, "free", offer/feature verbs.
_CLAIM_SIGNAL = re.compile(
    r"(\$\s?\d|\d+\s?%|\bfree\b|\bno (?:fees?|charge|cost)\b|"
    r"\bwe (?:offer|provide|charge|guarantee|include)\b|"
    r"\bour (?:platform|service|membership|pricing|plan|accounts?)\b|"
    r"\bper (?:month|year|gig|booking|session)\b)",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"[.!?\n]+")


def content_hash(draft: Draft) -> str:
    """Binds validated content to the claim (spec 7.3)."""
    return hashlib.sha256(f"{draft.subject}\x00{draft.body}".encode()).hexdigest()


def forbidden_hits(content: str, extra_patterns: tuple[str, ...] = ()) -> list[str]:
    """Matches against the forbidden-claims pack — reused by message validation
    and by collateral-asset registration (FR-3.10)."""
    hits = []
    for pattern in (*DEFAULT_FORBIDDEN, *extra_patterns):
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            hits.append(match.group(0))
    return hits


def unregistered_claims(content: str, approved: tuple[str, ...]) -> list[str]:
    """Claim-like sentences lacking any approved phrase (FR-3.2 allowlist) —
    reused by message validation and collateral-asset registration."""
    normalized = tuple(c.strip().lower() for c in approved)
    flagged = []
    for sentence in _SENTENCE_SPLIT.split(content):
        if not _CLAIM_SIGNAL.search(sentence):
            continue
        if not any(phrase in sentence.lower() for phrase in normalized):
            flagged.append(sentence.strip()[:80])
    return flagged


def validate(draft: Draft, ctx: ValidatorContext) -> list[Finding]:
    findings: list[Finding] = []
    subject, body = draft.subject.strip(), draft.body.strip()

    if not subject or not body:
        return [Finding(Violation.EMPTY, "empty subject or body")]

    if ctx.physical_address.strip() not in body:
        findings.append(
            Finding(Violation.MISSING_PHYSICAL_ADDRESS, "CAN-SPAM physical address absent")
        )
    if ctx.unsubscribe_text.strip().lower() not in body.lower():
        findings.append(Finding(Violation.MISSING_UNSUBSCRIBE, "unsubscribe text absent"))
    if ctx.sender_identity.strip() and ctx.sender_identity.strip() not in body:
        findings.append(
            Finding(Violation.MISSING_SENDER_IDENTITY, "sender identity absent from body")
        )

    if _FAKE_THREAD.match(subject):
        findings.append(Finding(Violation.FAKE_THREAD_SUBJECT, f"subject {subject!r}"))
    if _DECEPTIVE.search(subject):
        findings.append(Finding(Violation.DECEPTIVE_SUBJECT, f"subject {subject!r}"))

    if len(body.split()) > ctx.max_words:
        findings.append(
            Finding(Violation.TOO_LONG, f"{len(body.split())} words > {ctx.max_words}")
        )

    for hit in forbidden_hits(f"{subject}\n{body}", ctx.forbidden_patterns):
        findings.append(Finding(Violation.FORBIDDEN_CLAIM, hit))

    for url in _URL.findall(body):
        if not url.lower().startswith(tuple(p.lower() for p in ctx.allowed_url_prefixes)):
            findings.append(Finding(Violation.URL_NOT_ALLOWLISTED, url))

    if draft.step_index >= 1 and _BUMP_PATTERNS.search(body):
        findings.append(Finding(Violation.BUMP_THEATER, "content-free follow-up"))

    if ctx.sector_sensitivity != "none":
        # FR-3.11: information about a person under care never transits the
        # system. Deterministic battery; fail closed (I-11).
        from open_reachout.core.compliance.phi import phi_hits

        for hit in phi_hits(f"{subject}\n{body}"):
            findings.append(Finding(Violation.PHI_SUSPECTED, hit))

    if ctx.compliance_regime != "us_can_spam":
        # FR-7.7: regime extras are ADDITIVE — the core pack above always ran.
        from open_reachout.core.compliance.regimes import regime_findings

        findings.extend(regime_findings(ctx.compliance_regime, draft, ctx))

    if ctx.claim_mode == "allowlist":
        # FR-3.2: the agent cannot invent marketing claims — any claim-like
        # sentence must contain an approved, versioned claim phrase. The
        # compliance footer (address/unsubscribe lines) is exempt by position:
        # only sentences before the unsubscribe line are screened.
        cutoff = body.lower().find(ctx.unsubscribe_text.strip().lower())
        screened = body if cutoff < 0 else body[:cutoff]
        for sentence in unregistered_claims(screened, ctx.approved_claims):
            findings.append(Finding(Violation.UNREGISTERED_CLAIM, sentence))

    return findings
