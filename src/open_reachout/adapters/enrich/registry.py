"""Registry-evidence enricher: real, citable facts from the public-registry
record itself (no web scraping, no accounts).

The honest floor for personalization before Firecrawl/web enrichment is
configured: license type, credential, and practice location are real facts
with real provenance ("found via the NPPES registry"), which is exactly the
provenance-transparency opener the therapist use case runs (PRD Appendix A).
Anything richer (their website, specialties, accepting-clients status) waits
for web enrichment — the qualifier stays precision-biased either way.
"""

from __future__ import annotations

from datetime import UTC, datetime

from open_reachout.core.interfaces import Candidate, EvidenceCard, EvidenceFact

#: Human names for the taxonomy codes the examples use.
TAXONOMY_NAMES = {
    "106H00000X": "Licensed Marriage & Family Therapist",
    "101YP2500X": "Licensed Professional Counselor",
    "103T00000X": "Psychologist",
    "103TC0700X": "Clinical Psychologist",
    "103TP2701X": "Psychoanalysis Psychologist",
    "103G00000X": "Clinical Neuropsychologist",
    "1041C0700X": "Licensed Clinical Social Worker",
    "101YM0800X": "Mental Health Counselor",
    "101Y00000X": "Counselor",
}


class RegistryEnricher:
    """Implements core.interfaces.Enricher from registry data already on the
    candidate — every fact is verbatim from the public record."""

    name = "registry"

    def enrich(self, candidate: Candidate) -> EvidenceCard:
        now = datetime.now(UTC)
        ref = candidate.source_ref or {}
        npi = str(ref.get("npi", ""))
        source_url = (
            f"https://npiregistry.cms.hhs.gov/provider-view/{npi}"
            if npi else "https://npiregistry.cms.hhs.gov/"
        )
        raw_taxonomies = ref.get("taxonomies", [])
        taxonomies = (
            [str(t) for t in raw_taxonomies] if isinstance(raw_taxonomies, list) else []
        )
        location = candidate.address or ", ".join(
            str(v) for v in (ref.get("practice_city"), ref.get("practice_state")) if v
        )
        license_names = ", ".join(TAXONOMY_NAMES.get(t, t) for t in taxonomies)
        credential = str(ref.get("credential", "")).strip()
        facts = [
            EvidenceFact(
                fact_id="registry-provenance",
                fact_type="provenance",
                content=(
                    "Found via the public NPPES practitioner registry"
                    + (f" (NPI {npi})" if npi else "")
                    + " — government public data, not a purchased list."
                ),
                source_url=source_url,
                observed_at=now,
            ),
            EvidenceFact(
                fact_id="registry-license",
                fact_type="bio",
                content=(
                    f"Registry lists {candidate.display_name}"
                    + (f", {credential}" if credential else "")
                    + (f" as {license_names}" if license_names else "")
                    + (f"; practice location {location}" if location else "")
                    + "."
                ),
                source_url=source_url,
                observed_at=now,
            ),
        ]
        return EvidenceCard(prospect_ref=candidate.display_name, facts=facts)
