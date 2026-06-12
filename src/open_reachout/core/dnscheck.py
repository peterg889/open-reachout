"""DNS deliverability checks for own-domain sending (`reachout doctor`).

Evaluations are pure functions over TXT/MX record sets (fully tested); the
network resolver is an injectable callable (dnspython behind the `dns` extra).
These verify the Google/Microsoft bulk-sender floor from the research report:
SPF present and not +all, DMARC present with an enforcing-or-monitoring
policy, DKIM selector resolvable, MX present.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class DnsFinding:
    check: str
    severity: Severity
    detail: str


#: name -> TXT strings (empty list = no record / NXDOMAIN).
TxtLookup = Callable[[str], list[str]]
HasMx = Callable[[str], bool]


def evaluate_spf(txts: list[str]) -> DnsFinding:
    spf = [t for t in txts if t.lower().startswith("v=spf1")]
    if not spf:
        return DnsFinding("spf", Severity.FAIL, "no v=spf1 TXT record")
    if len(spf) > 1:
        return DnsFinding("spf", Severity.FAIL, "multiple SPF records (receivers reject)")
    record = spf[0].lower()
    if "+all" in record:
        return DnsFinding("spf", Severity.FAIL, "SPF '+all' authorizes the whole internet")
    if record.rstrip().endswith("?all"):
        return DnsFinding("spf", Severity.WARN, "SPF '?all' is neutral; prefer ~all or -all")
    return DnsFinding("spf", Severity.OK, record)


def evaluate_dmarc(txts: list[str]) -> DnsFinding:
    dmarc = [t for t in txts if t.lower().replace(" ", "").startswith("v=dmarc1")]
    if not dmarc:
        return DnsFinding(
            "dmarc", Severity.FAIL,
            "no _dmarc TXT record (required by Google/Microsoft bulk-sender rules)",
        )
    record = dmarc[0].lower().replace(" ", "")
    if "p=none" in record:
        return DnsFinding(
            "dmarc", Severity.OK,
            "p=none (the required minimum; consider quarantine once aligned)",
        )
    if "p=quarantine" in record or "p=reject" in record:
        return DnsFinding("dmarc", Severity.OK, record)
    return DnsFinding("dmarc", Severity.FAIL, f"DMARC record lacks a policy tag: {record}")


def evaluate_dkim(selector: str, txts: list[str]) -> DnsFinding:
    keyed = [t for t in txts if "p=" in t.replace(" ", "")]
    if not keyed:
        return DnsFinding(
            "dkim", Severity.FAIL, f"selector {selector!r}: no DKIM key record"
        )
    if any(t.replace(" ", "").rstrip(";").endswith("p=") for t in keyed):
        return DnsFinding(
            "dkim", Severity.FAIL, f"selector {selector!r}: revoked key (empty p=)"
        )
    return DnsFinding("dkim", Severity.OK, f"selector {selector!r} key present")


def check_domain(
    domain: str,
    lookup_txt: TxtLookup,
    has_mx: HasMx,
    *,
    dkim_selectors: tuple[str, ...] = ("google", "default", "selector1"),
) -> list[DnsFinding]:
    """All deliverability findings for one sending domain."""
    findings = [
        evaluate_spf(lookup_txt(domain)),
        evaluate_dmarc(lookup_txt(f"_dmarc.{domain}")),
    ]
    dkim: DnsFinding | None = None
    for selector in dkim_selectors:
        candidate = evaluate_dkim(selector, lookup_txt(f"{selector}._domainkey.{domain}"))
        dkim = candidate
        if candidate.severity is Severity.OK:
            break
    if dkim is not None:
        if dkim.severity is not Severity.OK:
            dkim = DnsFinding(
                "dkim", Severity.WARN,
                f"no DKIM key at common selectors {dkim_selectors} — pass your real "
                "selector if you use a different one",
            )
        findings.append(dkim)
    findings.append(
        DnsFinding("mx", Severity.OK, "MX present")
        if has_mx(domain)
        else DnsFinding("mx", Severity.FAIL, "no MX record (replies/bounces have nowhere to go)")
    )
    return findings


def live_lookups() -> tuple[TxtLookup, HasMx]:  # pragma: no cover - network
    """dnspython-backed resolvers (the `dns` extra)."""
    import dns.resolver

    def lookup_txt(name: str) -> list[str]:
        try:
            answers = dns.resolver.resolve(name, "TXT")
        except Exception:  # noqa: BLE001 — NXDOMAIN/timeout/etc: no record
            return []
        return [
            b"".join(r.strings).decode("utf-8", "replace")  # type: ignore[attr-defined]
            for r in answers
        ]

    def has_mx(domain: str) -> bool:
        try:
            return len(dns.resolver.resolve(domain, "MX")) > 0
        except Exception:  # noqa: BLE001
            return False

    return lookup_txt, has_mx
