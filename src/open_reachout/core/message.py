"""MIME assembly for own-domain sending (PRD FR-3.8, spec D-7).

The From, Message-ID, and List-Unsubscribe headers all sit on the caller's
own domain — this is what "send with the caller's domain" means concretely.
The Message-ID embeds the touch id so inbound replies and bounces (which echo
it in In-Reply-To / References) correlate back to the originating touch
without any provider-side lead mapping.

SPF/DKIM/DMARC are domain-DNS concerns the operator configures on their own
domain; `reachout doctor` checks them. This module guarantees the message-
level requirements: RFC 8058 one-click unsubscribe and a stable Message-ID.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr


def message_id_for(touch_id: str, domain: str) -> str:
    """`<touch-id@caller-domain>` — the correlation key for inbound mail."""
    return f"<{touch_id}@{domain}>"


def domain_of(mailbox: str) -> str:
    return mailbox.rpartition("@")[2]


def build_message(
    *,
    mailbox: str,
    sender_name: str,
    to_address: str,
    subject: str,
    body: str,
    touch_id: str,
    unsubscribe_url: str | None = None,
) -> EmailMessage:
    """Build a compliant plaintext message sent from the caller's mailbox.

    The mailbox (assigned by the gatekeeper) is on the caller's own domain;
    From and Reply-To are that mailbox. Raises ValueError on a mailbox that
    isn't a real address — fail closed before anything is dispatched.
    """
    domain = domain_of(mailbox)
    if not domain or "@" not in mailbox:
        raise ValueError(f"not a sendable mailbox: {mailbox!r}")

    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, mailbox))
    msg["To"] = to_address
    msg["Reply-To"] = formataddr((sender_name, mailbox))
    msg["Subject"] = subject
    msg["Message-ID"] = message_id_for(touch_id, domain)
    msg["Date"] = format_datetime(datetime.now(UTC))

    # RFC 8058 one-click unsubscribe. mailto always works (the caller's own
    # inbox); the https one-click is added when the operator runs the API.
    unsub = [f"<mailto:{mailbox}?subject=unsubscribe>"]
    if unsubscribe_url:
        unsub.insert(0, f"<{unsubscribe_url}>")
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["List-Unsubscribe"] = ", ".join(unsub)

    msg.set_content(body)
    return msg
