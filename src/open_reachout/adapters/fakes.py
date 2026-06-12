"""FakeProvider implementations of every interface (PRD FR-I.3).

These back the e2e harness, the injection corpus, and `dry-run` with zero
external calls. They are deliberately deterministic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

from pydantic import BaseModel

from open_reachout.core.gatekeeper import ClaimedTouch
from open_reachout.core.interfaces import (
    Candidate,
    ConfidenceBucket,
    DataBasis,
    DiscoverResult,
    EmailResult,
    EventKind,
    EvidenceCard,
    EvidenceFact,
    MailboxHealth,
    ProviderEvent,
    SendReceipt,
    VerifyResult,
    WebhookVerificationError,
)


class FakeSource:
    name = "fake_source"
    data_basis = DataBasis.GOVERNMENT_PUBLIC
    kind = "directory"

    def __init__(self, candidates: list[Candidate] | None = None) -> None:
        self._candidates = candidates or []

    def discover(
        self, cohort_filters: dict[str, object], cursor: str | None
    ) -> DiscoverResult:
        return DiscoverResult(candidates=list(self._candidates), cursor=None, cost_usd=0.0)


class FakeEnricher:
    def enrich(self, candidate: Candidate) -> EvidenceCard:
        return EvidenceCard(
            prospect_ref=candidate.display_name,
            facts=[
                EvidenceFact(
                    fact_id="fact-1",
                    fact_type="bio",
                    content=f"{candidate.display_name} runs {candidate.org_name or 'a business'}",
                    source_url=candidate.website or "https://example.test",
                    observed_at=datetime.now(UTC),
                )
            ],
        )


class FakeFinder:
    name = "fake_finder"

    def find(self, candidate: Candidate) -> EmailResult | None:
        if candidate.email_raw:
            return EmailResult(email=candidate.email_raw, provider=self.name)
        return None


class FakeVerifier:
    def verify(self, email: str) -> VerifyResult:
        bucket = (
            ConfidenceBucket.UNDELIVERABLE
            if email.startswith("bounce")
            else ConfidenceBucket.VERIFIED
        )
        return VerifyResult(email=email, bucket=bucket, confidence=0.99, provider="fake")


class FakeSendingProvider:
    """Records sends; verifies webhook HMAC like a real adapter must (I-10)."""

    def __init__(self, secret: bytes = b"fake-secret") -> None:
        self.secret = secret
        self.sent: list[tuple[ClaimedTouch, str, str]] = []
        self.paused_leads: list[str] = []
        self.paused_tenants: list[str] = []

    capability = "campaign"

    def send(self, message: ClaimedTouch, subject: str, body: str) -> SendReceipt:
        self.sent.append((message, subject, body))
        return SendReceipt(touch_id=message.touch_id, provider_ref={"fake_id": message.touch_id})

    def mailbox_health(self) -> list[MailboxHealth]:
        return [
            MailboxHealth(
                mailbox="fake@sender.test", warmup_complete=True, sent_today=0, daily_cap=25
            )
        ]

    def sign(self, payload: bytes) -> str:
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()

    def parse_webhook(self, payload: bytes, signature: str) -> list[ProviderEvent]:
        if not hmac.compare_digest(self.sign(payload), signature):
            raise WebhookVerificationError("bad signature")
        data = json.loads(payload)
        return [
            ProviderEvent(
                provider_event_id=str(data["id"]),
                kind=EventKind(data["kind"]),
                touch_ref=data.get("touch_ref", {}),
                payload=data.get("payload", {}),
            )
        ]

    def pause_lead(self, email_canonical: str) -> None:
        self.paused_leads.append(email_canonical)

    def pause_all_campaigns(self, tenant: str) -> None:
        self.paused_tenants.append(tenant)


class FakeLLM:
    """Returns canned structured outputs keyed by task name."""

    def __init__(self, canned: dict[str, dict[str, object]] | None = None) -> None:
        self.canned = canned or {}
        self.calls: list[tuple[str, str]] = []

    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        self.calls.append((task, prompt))
        if task not in self.canned:
            raise KeyError(f"FakeLLM has no canned output for task {task!r}")
        return schema.model_validate(self.canned[task])
