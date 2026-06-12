"""Own-domain SMTP sending provider (spec D-7, NG6 revision).

Sends each gatekeeper-claimed touch directly from the caller's own mailbox
(their Google Workspace / Microsoft 365 / self-hosted account) over
authenticated SMTP. This is the caller's own domain reputation — distinct
from the shared transactional ESP pools (SendGrid/SES) whose AUPs ban cold
email.

Capability `direct_send`: every send passes through the gatekeeper claim
transaction at dispatch time, so there is NO enrollment-then-reactive-pause
gap (contrast the provider-sequence mode in spec 7.6). Suppression and halt
are enforced transactionally before the SMTP socket opens, which is why
`pause_lead` / `pause_all_campaigns` are local no-ops here — there is nothing
scheduled provider-side to reactively pause.

Inbound (replies, bounces) arrives by IMAP polling, not webhooks; see
`adapters.sending.inbound`. `parse_webhook` is therefore unsupported.

Connection secrets come from the environment (never config files), as JSON
in OR_SMTP_MAILBOXES:
  {"outreach@get-brand.com": {"host": "smtp.gmail.com", "port": 587,
    "username": "...", "password": "...", "starttls": true}}
"""

from __future__ import annotations

import json
import os
import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage

from open_reachout.core.gatekeeper import ClaimedTouch
from open_reachout.core.interfaces import MailboxHealth, ProviderEvent, SendReceipt
from open_reachout.core.message import build_message

MAILBOXES_ENV = "OR_SMTP_MAILBOXES"


@dataclass(frozen=True)
class MailboxConn:
    host: str
    port: int
    username: str
    password: str
    sender_name: str = ""
    starttls: bool = True
    warmup_complete: bool = True
    daily_cap: int = 25


#: Transport seam: (conn, message) -> None. Real default below; tests inject.
Transport = Callable[[MailboxConn, EmailMessage], None]


def _smtp_transport(conn: MailboxConn, message: EmailMessage) -> None:  # pragma: no cover
    with smtplib.SMTP(conn.host, conn.port, timeout=30) as smtp:
        if conn.starttls:
            smtp.starttls()
        smtp.login(conn.username, conn.password)
        smtp.send_message(message)


def mailboxes_from_env() -> dict[str, MailboxConn]:
    raw = os.environ.get(MAILBOXES_ENV, "")
    if not raw:
        return {}
    return {
        addr: MailboxConn(**{k: v for k, v in spec.items()})
        for addr, spec in json.loads(raw).items()
    }


class SmtpSendingProvider:
    """Implements core.interfaces.SendingProvider with capability=direct_send."""

    capability = "direct_send"

    def __init__(
        self,
        mailboxes: dict[str, MailboxConn],
        *,
        transport: Transport = _smtp_transport,
        default_sender_name: str = "",
        unsubscribe_url: str | None = None,
    ) -> None:
        self.mailboxes = mailboxes
        self._transport = transport
        self.default_sender_name = default_sender_name
        self.unsubscribe_url = unsubscribe_url
        self.sent: list[tuple[str, str]] = []  # (mailbox, to) for observability

    def send(self, message: ClaimedTouch, subject: str, body: str) -> SendReceipt:
        conn = self.mailboxes.get(message.mailbox)
        if conn is None:
            # Fail closed: the gatekeeper assigned a mailbox we can't send from.
            raise RuntimeError(f"no SMTP credentials for mailbox {message.mailbox!r}")
        mime = build_message(
            mailbox=message.mailbox,
            sender_name=conn.sender_name or self.default_sender_name,
            to_address=message.recipient,
            subject=subject,
            body=body,
            touch_id=message.touch_id,
            unsubscribe_url=self.unsubscribe_url,
        )
        self._transport(conn, mime)
        self.sent.append((message.mailbox, message.recipient))
        return SendReceipt(
            touch_id=message.touch_id,
            provider_ref={"message_id": mime["Message-ID"], "mailbox": message.mailbox},
        )

    def mailbox_health(self) -> list[MailboxHealth]:
        # Advisory only (doctor); the authoritative daily cap is the
        # gatekeeper's mailbox_day counter.
        return [
            MailboxHealth(
                mailbox=addr, warmup_complete=c.warmup_complete,
                sent_today=0, daily_cap=c.daily_cap,
            )
            for addr, c in self.mailboxes.items()
        ]

    def parse_webhook(self, payload: bytes, signature: str) -> list[ProviderEvent]:
        raise NotImplementedError(
            "own-domain SMTP has no webhooks; inbound arrives via IMAP polling "
            "(adapters.sending.inbound)"
        )

    def pause_lead(self, email_canonical: str) -> None:
        # No-op: direct_send has no provider-side schedule to pause. The
        # gatekeeper refuses suppressed addresses at claim time (I-3).
        return None

    def pause_all_campaigns(self, tenant: str) -> None:
        # No-op: halt is enforced in the claim transaction (I-2); there is no
        # provider-side campaign that could keep sending.
        return None
