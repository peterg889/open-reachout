"""Signal-kind sources (PRD FR-2.2, spec 8.1): public-records feeds that
produce TIMING events rather than identities.

First implementation: new liquor/entertainment license filings. State ABC
boards publish these as CSVs; a new licensee is a venue about to open — a
perfect-timing outreach trigger ("a venue about to need live music").

The adapter deliberately reuses the FR-2.9 event machinery: each filing
becomes an operator event (deduped on jurisdiction+licensee+date) and fires
any `trigger: { event_type: ... }` cohort through the standard pipeline —
selector fields narrow the cohort's discovery, and every gate applies.
"""

from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core import queue

DEFAULT_EVENT_TYPE = "license.issued"

#: Flexible header matching: ABC-board exports vary; we accept the first
#: matching alias per field. `name` and one locality field are required.
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "licensee", "business_name", "trade_name", "dba"),
    "city": ("city", "locality", "town"),
    "state": ("state", "st", "province"),
    "issued": ("issued", "issue_date", "effective_date", "license_date"),
    "license_type": ("license_type", "type", "class"),
}


def _map_headers(fieldnames: list[str]) -> dict[str, str]:
    lowered = {f.lower().strip(): f for f in fieldnames}
    mapping: dict[str, str] = {}
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                mapping[field] = lowered[alias]
                break
    if "name" not in mapping:
        raise ValueError(
            f"no licensee-name column found (looked for {HEADER_ALIASES['name']}; "
            f"got {fieldnames})"
        )
    return mapping


def ingest_license_csv(
    conn: Connection, path: Path, *, event_type: str = DEFAULT_EVENT_TYPE
) -> tuple[int, int]:
    """Ingest a license-filings CSV as signal events. Returns
    (rows_read, events_fired). Re-ingesting the same file is a no-op:
    dedupe is jurisdiction + licensee + issue date."""
    fired = 0
    rows = 0
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        mapping = _map_headers(list(reader.fieldnames or []))

        def get(row: dict[str, str], field: str) -> str:
            col = mapping.get(field)
            return (row.get(col) or "").strip() if col else ""

        for row in reader:
            rows += 1
            name = get(row, "name")
            if not name:
                continue
            state = get(row, "state")
            issued = get(row, "issued")
            selector = {
                k: v for k, v in {
                    "business_name": name,
                    "city": get(row, "city"),
                    "state": state,
                    "license_type": get(row, "license_type"),
                }.items() if v
            }
            dedupe = f"signal:{event_type}:{state}:{name}:{issued}".lower()
            inserted = conn.execute(
                text(
                    """
                    INSERT INTO operator_events (event_type, selector, payload, dedupe_key)
                    VALUES (:e, CAST(:s AS jsonb), CAST(:p AS jsonb), :k)
                    ON CONFLICT (dedupe_key) DO NOTHING
                    RETURNING id
                    """
                ),
                {"e": event_type, "s": _json(selector),
                 "p": _json({"issued": issued, "source": str(path.name)}),
                 "k": dedupe},
            ).fetchone()
            if inserted is not None:
                queue.enqueue(
                    conn, "trigger", {"event_id": str(inserted[0])},
                    idempotency_key=f"trigger:{inserted[0]}",
                )
                fired += 1
    return rows, fired


def _json(obj: dict[str, str]) -> str:
    import json

    return json.dumps(obj)
