"""Typed variable registry and prompt-slot validation (PRD FR-3.1a, spec 9.6).

Variants are generation prompts with ``{{slot}}`` placeholders. Slots resolve
against a registry that assigns every variable a *trust class*:

- TRUSTED:   operator config (value props, voice rules, links)
- PROSPECT:  resolved identity fields (name, org)
- UNTRUSTED: anything that originated on the open web or from a stranger
             (evidence facts, signal payloads, thread excerpts)

Untrusted values are never spliced into prompt text. ``resolve`` replaces the
slot with an opaque reference marker and returns the values separately so the
LLM task builder can carry them inside the security envelope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

SLOT_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}")


class TrustClass(StrEnum):
    TRUSTED = "trusted"
    PROSPECT = "prospect"
    UNTRUSTED = "untrusted"


#: Default registry: exact names or ``prefix.*`` wildcards -> trust class.
DEFAULT_REGISTRY: dict[str, TrustClass] = {
    "persona.value_prop": TrustClass.TRUSTED,
    "persona.voice_rules": TrustClass.TRUSTED,
    "tenant.name": TrustClass.TRUSTED,
    "tenant.links.*": TrustClass.TRUSTED,
    "variant.*": TrustClass.TRUSTED,
    "prospect.first_name": TrustClass.PROSPECT,
    "prospect.org_name": TrustClass.PROSPECT,
    "prospect.city": TrustClass.PROSPECT,
    "asset.*": TrustClass.TRUSTED,  # operator-vetted collateral links (FR-3.10)
    "sender.*": TrustClass.TRUSTED,  # human-approved sender facts (FR-0.7)
    "evidence.*": TrustClass.UNTRUSTED,
    "signal.*": TrustClass.UNTRUSTED,
    "thread.*": TrustClass.UNTRUSTED,
}


class UnknownSlotError(ValueError):
    def __init__(self, slots: list[str]) -> None:
        super().__init__(f"unknown variable slot(s): {', '.join(sorted(slots))}")
        self.slots = slots


def extract_slots(prompt: str) -> list[str]:
    """All distinct slots in *prompt*, in order of first appearance."""
    seen: dict[str, None] = {}
    for match in SLOT_RE.finditer(prompt):
        seen.setdefault(match.group(1), None)
    return list(seen)


def lookup(slot: str, registry: dict[str, TrustClass] | None = None) -> TrustClass | None:
    reg = registry if registry is not None else DEFAULT_REGISTRY
    if slot in reg:
        return reg[slot]
    parts = slot.split(".")
    for i in range(len(parts) - 1, 0, -1):
        wildcard = ".".join(parts[:i]) + ".*"
        if wildcard in reg:
            return reg[wildcard]
    return None


def validate_prompt(prompt: str, registry: dict[str, TrustClass] | None = None) -> list[str]:
    """Return the list of unknown slots (empty == valid). Fail closed upstream."""
    return [s for s in extract_slots(prompt) if lookup(s, registry) is None]


def marker_for(slot: str) -> str:
    """Opaque reference marker substituted for untrusted slots (spec 9.6)."""
    return f"[UNTRUSTED-REF:{slot}]"


@dataclass(frozen=True)
class ResolvedValue:
    slot: str
    value: str
    trust: TrustClass
    fact_id: str | None = None  # evidence provenance, when applicable
    source_url: str | None = None


@dataclass(frozen=True)
class ResolvedPrompt:
    """Prompt text with trusted/prospect values inlined and untrusted values
    replaced by markers; the untrusted values travel separately (envelope)."""

    text: str
    untrusted: list[ResolvedValue] = field(default_factory=list)
    resolved: list[ResolvedValue] = field(default_factory=list)  # full trace snapshot


def resolve(
    prompt: str,
    values: dict[str, ResolvedValue],
    registry: dict[str, TrustClass] | None = None,
) -> ResolvedPrompt:
    """Interpolate *values* into *prompt* per trust class.

    Raises :class:`UnknownSlotError` for unregistered slots and ``KeyError``
    for registered slots with no provided value — both fail closed.
    """
    unknown = validate_prompt(prompt, registry)
    if unknown:
        raise UnknownSlotError(unknown)

    untrusted: list[ResolvedValue] = []
    trace: list[ResolvedValue] = []

    def substitute(match: re.Match[str]) -> str:
        slot = match.group(1)
        trust = lookup(slot, registry)
        assert trust is not None  # validated above
        rv = values[slot]
        if rv.trust is not trust:
            raise ValueError(f"value for {slot!r} declared {rv.trust}, registry says {trust}")
        trace.append(rv)
        if trust is TrustClass.UNTRUSTED:
            untrusted.append(rv)
            return marker_for(slot)
        return rv.value

    text = SLOT_RE.sub(substitute, prompt)
    return ResolvedPrompt(text=text, untrusted=untrusted, resolved=trace)
