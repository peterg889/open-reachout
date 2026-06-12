"""IMAP inbound parsing for own-domain SMTP (spec 8.5).

Replies and bounces don't arrive as webhooks when you send from your own
mailbox — they land in that mailbox. This module turns a raw RFC822 message
into the same ProviderEvent the webhook path produces, so the deterministic
event handling in core.events runs identically for both transports.

Correlation: outbound carries Message-ID `<touch-id@domain>`; replies echo it
in In-Reply-To/References and bounces (DSNs) quote it. We recover the touch
id from there — no provider-side lead mapping needed.

The `parse_inbound` function is pure and fully tested; the IMAP fetch loop is
a thin shell (`poll_inbox`) that calls it.
"""

from __future__ import annotations

import re
import uuid
from email import message_from_bytes
from email.message import Message
from email.policy import default as default_policy

from open_reachout.core.interfaces import EventKind, ProviderEvent

_TOUCH_RE = re.compile(r"<([0-9a-f-]{36})@", re.IGNORECASE)
_BOUNCE_SENDER = re.compile(r"mailer-daemon|postmaster|no-?reply", re.IGNORECASE)


def _touch_id_from(*header_values: str | None) -> str | None:
    for value in header_values:
        if not value:
            continue
        for match in _TOUCH_RE.finditer(value):
            try:
                return str(uuid.UUID(match.group(1)))
            except ValueError:
                continue
    return None


def _is_bounce(msg: Message) -> bool:
    ctype = msg.get_content_type()
    if ctype == "multipart/report" and "delivery-status" in (
        msg.get_param("report-type") or ""
    ):
        return True
    return bool(_BOUNCE_SENDER.search(msg.get("From", "")))


def _plain_body(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_content().strip()
                except (LookupError, ValueError):
                    continue
        return ""
    try:
        return (msg.get_content() or "").strip()
    except (LookupError, ValueError):
        return ""


def _original_message_id(msg: Message) -> str | None:
    """For a DSN, dig the failed message's id out of the nested report parts."""
    direct = _touch_id_from(msg.get("In-Reply-To"), msg.get("References"))
    if direct:
        return direct
    for part in msg.walk():
        if part.get_content_type() in ("message/rfc822", "text/rfc822-headers",
                                       "message/delivery-status"):
            try:
                nested = part.get_content()
            except (LookupError, ValueError):
                nested = str(part.get_payload())
            found = _touch_id_from(str(nested))
            if found:
                return found
    return None


def parse_inbound(raw: bytes) -> ProviderEvent | None:
    """Raw RFC822 -> ProviderEvent (reply or bounce), or None if it can't be
    correlated to a touch. Pure and deterministic (gate-tested)."""
    msg = message_from_bytes(raw, policy=default_policy)
    event_id = msg.get("Message-ID") or f"inbound:{hash(raw)}"

    if _is_bounce(msg):
        touch_id = _original_message_id(msg)
        if touch_id is None:
            return None
        return ProviderEvent(
            provider_event_id=event_id, kind=EventKind.BOUNCE,
            touch_ref={"touch_id": touch_id}, payload={"transport": "smtp"},
        )

    touch_id = _touch_id_from(msg.get("In-Reply-To"), msg.get("References"))
    if touch_id is None:
        return None  # unsolicited mail to the mailbox: not ours to act on
    return ProviderEvent(
        provider_event_id=event_id, kind=EventKind.REPLY,
        touch_ref={"touch_id": touch_id}, payload={"body": _plain_body(msg)},
    )


def parse_batch(raws: list[bytes]) -> list[ProviderEvent]:
    return [e for raw in raws if (e := parse_inbound(raw)) is not None]
