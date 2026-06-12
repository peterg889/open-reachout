"""Plugin interfaces (PRD section 5). Adapters implement these Protocols and
register via entry points; `adapter_conformance/` (M1) ships the reusable
conformance suite every implementation must pass."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from open_reachout.core.gatekeeper import ClaimedTouch


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DataBasis(StrEnum):
    GOVERNMENT_PUBLIC = "government_public"
    LICENSED = "licensed"
    OWN_SITE_SCRAPE = "own_site_scrape"
    API_TERMS = "api_terms"
    REFERRAL = "referral"
    IMPORTED = "imported"


class Candidate(_Model):
    display_name: str
    org_name: str | None = None
    website: str | None = None
    email_raw: str | None = None
    phone: str | None = None
    address: str | None = None
    source_adapter: str
    source_ref: dict[str, object] = Field(default_factory=dict)
    data_basis: DataBasis


class DiscoverResult(_Model):
    candidates: list[Candidate]
    cursor: str | None = None
    cost_usd: float = 0.0


class EvidenceFact(_Model):
    fact_id: str
    fact_type: str
    content: str
    source_url: str
    observed_at: datetime


class EvidenceCard(_Model):
    prospect_ref: str
    facts: list[EvidenceFact]


class EmailResult(_Model):
    email: str
    provider: str
    cost_usd: float = 0.0


class ConfidenceBucket(StrEnum):
    VERIFIED = "verified"
    RISKY = "risky"
    UNDELIVERABLE = "undeliverable"
    UNKNOWN = "unknown"


class VerifyResult(_Model):
    email: str
    bucket: ConfidenceBucket
    confidence: float = Field(ge=0.0, le=1.0)
    provider: str


class SendReceipt(_Model):
    touch_id: str
    provider_ref: dict[str, str] = Field(default_factory=dict)


class MailboxHealth(_Model):
    mailbox: str
    warmup_complete: bool
    sent_today: int
    daily_cap: int


class EventKind(StrEnum):
    REPLY = "reply"
    BOUNCE = "bounce"
    COMPLAINT = "complaint"
    UNSUBSCRIBE = "unsubscribe"
    SENT = "sent"
    DELIVERED = "delivered"


class ProviderEvent(_Model):
    provider_event_id: str
    kind: EventKind
    touch_ref: dict[str, str] = Field(default_factory=dict)
    payload: dict[str, object] = Field(default_factory=dict)


class WebhookVerificationError(Exception):
    """Unsigned/invalid webhook (invariant I-10): drop + alert, never process."""


# ----------------------------------------------------------------- protocols
@runtime_checkable
class SourceAdapter(Protocol):
    name: str
    data_basis: DataBasis
    kind: str  # "directory" | "signal"

    def discover(self, cohort_filters: dict[str, object], cursor: str | None) -> DiscoverResult: ...


@runtime_checkable
class Enricher(Protocol):
    def enrich(self, candidate: Candidate) -> EvidenceCard: ...


@runtime_checkable
class EmailFinder(Protocol):
    name: str

    def find(self, candidate: Candidate) -> EmailResult | None: ...


@runtime_checkable
class Verifier(Protocol):
    def verify(self, email: str) -> VerifyResult: ...


@runtime_checkable
class SendingProvider(Protocol):
    """Note the parameter type: nothing but a ClaimedTouch is sendable (I-1)."""

    def send(self, message: ClaimedTouch, subject: str, body: str) -> SendReceipt: ...
    def mailbox_health(self) -> list[MailboxHealth]: ...
    def parse_webhook(self, payload: bytes, signature: str) -> list[ProviderEvent]: ...
    def pause_lead(self, email_canonical: str) -> None: ...
    def pause_all_campaigns(self, tenant: str) -> None: ...


@runtime_checkable
class LLMBackend(Protocol):
    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel: ...
