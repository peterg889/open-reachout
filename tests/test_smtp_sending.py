"""Own-domain SMTP sending + inbound parsing (spec D-7)."""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from open_reachout.adapters.sending.inbound import parse_batch, parse_inbound
from open_reachout.adapters.sending.smtp import MailboxConn, SmtpSendingProvider
from open_reachout.core.interfaces import EventKind
from open_reachout.core.message import build_message, message_id_for

TOUCH = "0b6cda1c-9d5e-4cb9-a5e9-1234567890ab"
MAILBOX = "outreach@get-stagematch.com"


def test_message_built_on_caller_domain() -> None:
    msg = build_message(
        mailbox=MAILBOX, sender_name="Maya Reyes", to_address="owner@venue.test",
        subject="Live music on Thursdays", body="Hi there.\n- Maya", touch_id=TOUCH,
        unsubscribe_url="https://get-stagematch.com/u/abc",
    )
    # From and the Message-ID both live on the caller's own domain.
    assert "get-stagematch.com" in msg["From"]
    assert msg["Message-ID"] == message_id_for(TOUCH, "get-stagematch.com")
    # RFC 8058 one-click unsubscribe.
    assert "List-Unsubscribe-Post" in msg
    assert "https://get-stagematch.com/u/abc" in msg["List-Unsubscribe"]
    assert f"mailto:{MAILBOX}" in msg["List-Unsubscribe"]


def test_message_without_url_still_has_mailto_unsub() -> None:
    msg = build_message(
        mailbox=MAILBOX, sender_name="Maya", to_address="x@y.test",
        subject="s", body="b", touch_id=TOUCH,
    )
    assert "List-Unsubscribe-Post" not in msg  # only with one-click URL
    assert msg["List-Unsubscribe"].startswith(f"<mailto:{MAILBOX}")


def _claimed():
    from open_reachout.core import gatekeeper

    gatekeeper._claim_guard.token = gatekeeper._CONSTRUCTION_TOKEN
    try:
        return gatekeeper.ClaimedTouch(
            touch_id=TOUCH, tenant="stagematch", mailbox=MAILBOX,
            content_sha256="h", recipient="owner@venue.test",
        )
    finally:
        gatekeeper._claim_guard.token = None


def test_send_routes_to_assigned_mailbox() -> None:
    captured: list = []
    provider = SmtpSendingProvider(
        {MAILBOX: MailboxConn("smtp.test", 587, "u", "p", sender_name="Maya Reyes")},
        transport=lambda conn, msg: captured.append((conn, msg)),
    )
    receipt = provider.send(_claimed(), "subject", "body\n- Maya")
    assert len(captured) == 1
    conn, mime = captured[0]
    assert conn.host == "smtp.test"
    assert mime["To"] == "owner@venue.test"
    assert "get-stagematch.com" in mime["From"]
    assert receipt.provider_ref["mailbox"] == MAILBOX


def test_send_fails_closed_on_unknown_mailbox() -> None:
    provider = SmtpSendingProvider({}, transport=lambda c, m: None)
    with pytest.raises(RuntimeError, match="no SMTP credentials"):
        provider.send(_claimed(), "s", "b")


def test_webhooks_unsupported_pauses_are_local_noops() -> None:
    provider = SmtpSendingProvider({MAILBOX: MailboxConn("h", 1, "u", "p")},
                                   transport=lambda c, m: None)
    assert provider.capability == "direct_send"
    with pytest.raises(NotImplementedError):
        provider.parse_webhook(b"{}", "sig")
    # No-ops: enforcement is transactional (I-2/I-3), nothing to pause.
    assert provider.pause_lead("x@y.test") is None
    assert provider.pause_all_campaigns("stagematch") is None


# ----------------------------------------------------------------- inbound
def _reply_email(in_reply_to: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "owner@venue.test"
    msg["To"] = MAILBOX
    msg["Message-ID"] = "<reply-1@venue.test>"
    msg["In-Reply-To"] = in_reply_to
    msg["Subject"] = "Re: Live music"
    msg.set_content("Sure, tell me more about pricing.")
    return msg.as_bytes()


def test_inbound_reply_correlates_by_message_id() -> None:
    event = parse_inbound(_reply_email(message_id_for(TOUCH, "get-stagematch.com")))
    assert event is not None
    assert event.kind is EventKind.REPLY
    assert event.touch_ref == {"touch_id": TOUCH}
    assert "pricing" in event.payload["body"]
    assert event.provider_event_id == "<reply-1@venue.test>"


def test_inbound_uncorrelated_reply_ignored() -> None:
    msg = EmailMessage()
    msg["From"] = "stranger@elsewhere.test"
    msg["Message-ID"] = "<x@elsewhere.test>"
    msg.set_content("cold inbound, not ours")
    assert parse_inbound(msg.as_bytes()) is None


def test_inbound_bounce_detected_from_dsn() -> None:
    outer = EmailMessage()
    outer["From"] = "MAILER-DAEMON@get-stagematch.com"
    outer["To"] = MAILBOX
    outer["Message-ID"] = "<dsn-1@get-stagematch.com>"
    outer["Subject"] = "Undelivered Mail Returned to Sender"
    outer["In-Reply-To"] = message_id_for(TOUCH, "get-stagematch.com")
    outer.set_content("Delivery failed permanently.")
    event = parse_inbound(outer.as_bytes())
    assert event is not None and event.kind is EventKind.BOUNCE
    assert event.touch_ref == {"touch_id": TOUCH}


def test_parse_batch_filters_uncorrelated() -> None:
    good = _reply_email(message_id_for(TOUCH, "get-stagematch.com"))
    bad = b"From: x@y.test\r\nSubject: spam\r\n\r\nhi"
    assert len(parse_batch([good, bad])) == 1
