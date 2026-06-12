"""IMAP polling pipeline (Postgres) and pure DNS deliverability checks."""

from __future__ import annotations

from email.message import EmailMessage

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from tests.conftest import Seed
from tests.test_worker_e2e_pg import make_worker, queue_one

from open_reachout.adapters.fakes import FakeSendingProvider
from open_reachout.adapters.sending.imap_poll import ImapConn, poll_once
from open_reachout.core.dnscheck import (
    Severity,
    check_domain,
    evaluate_dkim,
    evaluate_dmarc,
    evaluate_spf,
)
from open_reachout.core.message import message_id_for


# ----------------------------------------------------------------- IMAP poll
def _reply_raw(touch_id: str, body: str, msg_id: str = "<r1@venue.test>") -> bytes:
    msg = EmailMessage()
    msg["From"] = "owner@venue.test"
    msg["To"] = "outreach@try-stagematch.com"
    msg["Message-ID"] = msg_id
    msg["In-Reply-To"] = message_id_for(touch_id, "try-stagematch.com")
    msg["Subject"] = "Re: Live music"
    msg.set_content(body)
    return msg.as_bytes()


@pytest.mark.postgres
def test_poll_feeds_the_same_pipeline_as_webhooks(
    pg_engine: Engine, conn, seed: Seed
) -> None:
    conn.commit()
    touch_id = queue_one(pg_engine, seed)
    provider = FakeSendingProvider()
    make_worker(pg_engine, provider).drain()

    inbox = {"outreach@try-stagematch.com": ImapConn(host="imap.test")}
    raws = [
        _reply_raw(touch_id, "Tell me more about pricing?"),
        b"From: stranger@x.test\r\nSubject: spam\r\n\r\nunrelated",  # uncorrelated
    ]
    processed = poll_once(pg_engine, inbox, fetcher=lambda c: raws)
    assert processed == 1
    # Redelivery after a crash (seen-flag lost): Message-ID dedupe makes it a
    # no-op (I-10), exactly like a duplicated webhook.
    assert poll_once(pg_engine, inbox, fetcher=lambda c: raws) == 0

    with pg_engine.begin() as c:
        assert c.execute(text("SELECT state FROM prospects")).scalar() == "engaged"
        assert c.execute(text("SELECT count(*) FROM jobs WHERE queue='classify'")).scalar() == 1


@pytest.mark.postgres
def test_polled_unsubscribe_reply_suppresses_deterministically(
    pg_engine: Engine, conn, seed: Seed
) -> None:
    conn.commit()
    touch_id = queue_one(pg_engine, seed)
    provider = FakeSendingProvider()
    worker = make_worker(pg_engine, provider)
    worker.drain()
    inbox = {"outreach@try-stagematch.com": ImapConn(host="imap.test")}
    raws = [_reply_raw(touch_id, "please take me off your list", "<u1@venue.test>")]
    poll_once(pg_engine, inbox, fetcher=lambda c: raws)
    worker.drain()  # classify job runs the deterministic pre-pass
    with pg_engine.begin() as c:
        assert c.execute(text("SELECT state FROM prospects")).scalar() == "unsubscribed"


# ----------------------------------------------------------------- DNS checks
def test_spf_evaluations() -> None:
    assert evaluate_spf([]).severity is Severity.FAIL
    assert evaluate_spf(["v=spf1 include:_spf.google.com ~all"]).severity is Severity.OK
    assert evaluate_spf(["v=spf1 +all"]).severity is Severity.FAIL
    assert evaluate_spf(["v=spf1 a ?all"]).severity is Severity.WARN
    two = evaluate_spf(["v=spf1 ~all", "v=spf1 -all"])
    assert two.severity is Severity.FAIL and "multiple" in two.detail


def test_dmarc_evaluations() -> None:
    assert evaluate_dmarc([]).severity is Severity.FAIL
    assert evaluate_dmarc(["v=DMARC1; p=none; rua=mailto:d@x.test"]).severity is Severity.OK
    assert evaluate_dmarc(["v=DMARC1; p=quarantine"]).severity is Severity.OK
    assert evaluate_dmarc(["v=DMARC1; rua=mailto:d@x.test"]).severity is Severity.FAIL


def test_dkim_evaluations() -> None:
    assert evaluate_dkim("google", []).severity is Severity.FAIL
    ok = evaluate_dkim("google", ["v=DKIM1; k=rsa; p=MIGfMA0GCSq..."])
    assert ok.severity is Severity.OK
    revoked = evaluate_dkim("google", ["v=DKIM1; k=rsa; p="])
    assert revoked.severity is Severity.FAIL and "revoked" in revoked.detail


def test_check_domain_full_pass_and_failures() -> None:
    records = {
        "try-stagematch.com": ["v=spf1 include:_spf.google.com ~all"],
        "_dmarc.try-stagematch.com": ["v=DMARC1; p=none"],
        "google._domainkey.try-stagematch.com": ["v=DKIM1; p=MIGfMA0"],
    }
    findings = check_domain(
        "try-stagematch.com", lambda n: records.get(n, []), lambda d: True
    )
    assert all(f.severity is Severity.OK for f in findings)

    # Bare domain: everything that matters fails loudly.
    bare = check_domain("new-domain.test", lambda n: [], lambda d: False)
    by_check = {f.check: f.severity for f in bare}
    assert by_check["spf"] is Severity.FAIL
    assert by_check["dmarc"] is Severity.FAIL
    assert by_check["dkim"] is Severity.WARN  # unknown selector: warn, not fail
    assert by_check["mx"] is Severity.FAIL
