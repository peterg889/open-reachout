"""NPPES NPI Registry source adapter (PRD FR-2.1, research report section 4).

Reads the public CMS NPPES Data Dissemination CSV (the operator downloads the
bulk file; it is free and public). Filters individual providers (entity type
1) by practice state and taxonomy codes.

Bright-line rule (the CareDash rule, PRD Appendix A): this data feeds private
outreach targeting only — never public profile pre-population. NPPES does not
contain email addresses; email discovery happens in enrichment, primarily from
the prospect's own website.
"""

from __future__ import annotations

import csv
from pathlib import Path

from open_reachout.core.interfaces import Candidate, DataBasis, DiscoverResult

# Column names from the NPPES Data Dissemination file header.
_NPI = "NPI"
_ENTITY_TYPE = "Entity Type Code"
_FIRST = "Provider First Name"
_LAST = "Provider Last Name (Legal Name)"
_CRED = "Provider Credential Text"
_ADDR1 = "Provider First Line Business Practice Location Address"
_CITY = "Provider Business Practice Location Address City Name"
_STATE = "Provider Business Practice Location Address State Name"
_ZIP = "Provider Business Practice Location Address Postal Code"
_PHONE = "Provider Business Practice Location Address Telephone Number"
_TAXONOMY_COLS = [f"Healthcare Provider Taxonomy Code_{i}" for i in range(1, 16)]

_INDIVIDUAL = "1"
_BATCH = 500


class NppesSource:
    name = "nppes"
    data_basis = DataBasis.GOVERNMENT_PUBLIC
    kind = "directory"

    def __init__(self, csv_path: Path) -> None:
        self.csv_path = csv_path

    def discover(
        self, cohort_filters: dict[str, object], cursor: str | None
    ) -> DiscoverResult:
        """Stream candidates matching `{state, taxonomy: [...]}` filters.

        Cursor = byte offset into the file, so repeated calls page through the
        multi-GB dissemination file without rereading it.
        """
        state = str(cohort_filters.get("state", "")).upper()
        taxonomies = {str(t) for t in cohort_filters.get("taxonomy", []) or []}  # type: ignore[union-attr]
        if not state or not taxonomies:
            raise ValueError("nppes cohort filters require 'state' and 'taxonomy'")

        candidates: list[Candidate] = []
        with open(self.csv_path, newline="", encoding="utf-8") as fh:
            header = next(csv.reader([fh.readline()]))
            if cursor:
                fh.seek(int(cursor))
            offset = fh.tell()
            for line in fh:
                offset += len(line.encode("utf-8"))
                row = dict(zip(header, next(csv.reader([line])), strict=False))
                candidate = self._match(row, state, taxonomies)
                if candidate:
                    candidates.append(candidate)
                if len(candidates) >= _BATCH:
                    return DiscoverResult(candidates=candidates, cursor=str(offset))
        return DiscoverResult(candidates=candidates, cursor=None)

    def _match(
        self, row: dict[str, str], state: str, taxonomies: set[str]
    ) -> Candidate | None:
        if row.get(_ENTITY_TYPE) != _INDIVIDUAL:
            return None
        if (row.get(_STATE) or "").upper() != state:
            return None
        row_taxonomies = {row.get(col, "") for col in _TAXONOMY_COLS} - {""}
        if not (row_taxonomies & taxonomies):
            return None
        first, last = (row.get(_FIRST) or "").title(), (row.get(_LAST) or "").title()
        if not (first and last):
            return None
        return Candidate(
            display_name=f"{first} {last}",
            org_name=None,
            website=None,  # discovered in enrichment from name+city web research
            email_raw=None,  # NPPES has no emails — enrichment's job
            phone=row.get(_PHONE) or None,
            address=", ".join(
                p for p in (row.get(_ADDR1), row.get(_CITY), row.get(_STATE), row.get(_ZIP)) if p
            ),
            source_adapter=self.name,
            source_ref={
                "npi": row.get(_NPI, ""),
                "taxonomies": sorted(row_taxonomies & taxonomies),
                "credential": row.get(_CRED) or "",
            },
            data_basis=self.data_basis,
        )
