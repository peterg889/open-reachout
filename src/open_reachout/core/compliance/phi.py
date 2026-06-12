"""Sector-sensitivity screen (PRD FR-3.11, spec 13.6).

For healthcare-adjacent tenants, content that reads as information about a
person under care must never transit the system — not in outbound mail, not
in operator-supplied payloads. This is the deterministic pattern battery; it
alone is sufficient to block (no LLM on the rejection path, I-11). Precision
over recall: every pattern here is a strong PHI shape, because a false
positive blocks legitimate provider-to-provider outreach (spec OQ-8 tracks
threshold tuning against a labeled corpus).
"""

from __future__ import annotations

import re

#: High-precision PHI shapes. Tenants extend via config; never shrink.
PHI_PATTERNS: tuple[tuple[str, str], ...] = (
    ("dob", r"\b(?:DOB|date of birth)\b.{0,24}\d"),
    ("record_id", r"\b(?:MRN|medical record (?:number|#)|member id|policy (?:number|#))"
                  r"\b.{0,16}[A-Za-z0-9-]{4,}"),
    ("icd_code", r"\b[A-TV-Z]\d{2}\.\d{1,3}\b"),
    ("diagnosis", r"\bdiagnos(?:ed|is of)\b.{0,40}"),
    ("care_detail", r"\b(?:session notes|treatment plan for|intake notes|"
                    r"presenting problem)\b"),
    ("care_relationship", r"\bmy (?:patient|client)\b.{0,40}"),
)


def phi_hits(content: str) -> list[str]:
    """Pattern names that matched — empty list is the only pass."""
    hits: list[str] = []
    for name, pattern in PHI_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            hits.append(name)
    return hits
