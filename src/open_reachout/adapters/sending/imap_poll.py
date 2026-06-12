"""IMAP inbound polling for own-domain sending (spec 8.5).

Fetches unseen mail from the operator's own mailboxes, parses it with
`adapters.sending.inbound`, and feeds the same `ingest_events` path the
webhook route uses — replies, bounces, and dedupe behave identically.

Credentials come from the environment (never config files), as JSON in
OR_IMAP_MAILBOXES:
  {"outreach@get-brand.com": {"host": "imap.gmail.com", "port": 993,
    "username": "...", "password": "..."}}

The IMAP socket work is behind an injectable fetcher so everything above it
is tested without a server.
"""

from __future__ import annotations

import imaplib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.engine import Engine

from open_reachout.adapters.sending.inbound import parse_batch
from open_reachout.core.events import ingest_events

MAILBOXES_ENV = "OR_IMAP_MAILBOXES"


@dataclass(frozen=True)
class ImapConn:
    host: str
    port: int = 993
    username: str = ""
    password: str = ""
    folder: str = "INBOX"


#: Fetcher seam: returns raw RFC822 messages and marks them seen server-side.
Fetcher = Callable[[ImapConn], list[bytes]]


def _imap_fetch_unseen(conn: ImapConn) -> list[bytes]:  # pragma: no cover - network
    raws: list[bytes] = []
    with imaplib.IMAP4_SSL(conn.host, conn.port) as imap:
        imap.login(conn.username, conn.password)
        imap.select(conn.folder)
        _status, data = imap.search(None, "UNSEEN")
        for num in (data[0] or b"").split():
            _status, parts = imap.fetch(num, "(RFC822)")
            for part in parts:
                if isinstance(part, tuple) and len(part) >= 2:
                    raws.append(part[1])
            imap.store(num, "+FLAGS", "\\Seen")
    return raws


def mailboxes_from_env() -> dict[str, ImapConn]:
    raw = os.environ.get(MAILBOXES_ENV, "")
    if not raw:
        return {}
    return {addr: ImapConn(**spec) for addr, spec in json.loads(raw).items()}


def poll_once(
    engine: Engine,
    mailboxes: dict[str, ImapConn],
    *,
    fetcher: Fetcher = _imap_fetch_unseen,
) -> int:
    """Fetch + parse + ingest across all inboxes. Returns events processed.

    Idempotent against redelivery: ingest dedupes on Message-ID, so a message
    re-fetched after a crash (seen-flag not yet stored) is a no-op (I-10).
    """
    processed = 0
    for conn in mailboxes.values():
        events = parse_batch(fetcher(conn))
        if not events:
            continue
        with engine.begin() as db:
            processed += ingest_events(db, events)
    return processed
