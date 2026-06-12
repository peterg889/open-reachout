"""Prospect lifecycle state machine (PRD section 6, spec section 5.4).

``prospects.state`` may only be written through :func:`assert_transition`'s
caller, ``core.lifecycle.transition`` (enforced by a CI grep-test). This module
holds the pure rules so they are trivially testable.
"""

from __future__ import annotations

from enum import StrEnum


class ProspectState(StrEnum):
    DISCOVERED = "discovered"
    ENRICHED = "enriched"
    QUALIFIED = "qualified"
    QUEUED = "queued"
    CONTACTED = "contacted"
    ENGAGED = "engaged"
    CONVERTED = "converted"
    # exits
    DISQUALIFIED = "disqualified"
    UNENRICHABLE = "unenrichable"
    BOUNCED = "bounced"
    DECLINED = "declined"
    UNSUBSCRIBED = "unsubscribed"
    NO_RESPONSE = "no_response"
    FORGOTTEN = "forgotten"


#: Allowed transitions. Anything not listed is a bug, not a feature request.
TRANSITIONS: dict[ProspectState, frozenset[ProspectState]] = {
    ProspectState.DISCOVERED: frozenset(
        {ProspectState.ENRICHED, ProspectState.UNENRICHABLE, ProspectState.FORGOTTEN}
    ),
    ProspectState.ENRICHED: frozenset(
        {ProspectState.QUALIFIED, ProspectState.DISQUALIFIED, ProspectState.FORGOTTEN}
    ),
    ProspectState.QUALIFIED: frozenset({ProspectState.QUEUED, ProspectState.FORGOTTEN}),
    ProspectState.QUEUED: frozenset(
        {
            ProspectState.CONTACTED,
            ProspectState.DISQUALIFIED,  # late suppression/frequency arbitration
            ProspectState.FORGOTTEN,
        }
    ),
    ProspectState.CONTACTED: frozenset(
        {
            ProspectState.ENGAGED,
            ProspectState.BOUNCED,
            ProspectState.DECLINED,
            ProspectState.UNSUBSCRIBED,
            ProspectState.NO_RESPONSE,
            ProspectState.FORGOTTEN,
        }
    ),
    ProspectState.ENGAGED: frozenset(
        {
            ProspectState.CONVERTED,
            ProspectState.DECLINED,
            ProspectState.UNSUBSCRIBED,
            ProspectState.NO_RESPONSE,
            ProspectState.FORGOTTEN,
        }
    ),
    # 90-day cooldown then one re-eligibility (PRD section 6): re-enters at QUEUED.
    ProspectState.NO_RESPONSE: frozenset({ProspectState.QUEUED, ProspectState.FORGOTTEN}),
    # Terminal states: only `forget` may follow.
    ProspectState.CONVERTED: frozenset({ProspectState.FORGOTTEN}),
    ProspectState.DISQUALIFIED: frozenset({ProspectState.FORGOTTEN}),
    ProspectState.UNENRICHABLE: frozenset({ProspectState.FORGOTTEN}),
    ProspectState.BOUNCED: frozenset({ProspectState.FORGOTTEN}),
    ProspectState.DECLINED: frozenset({ProspectState.FORGOTTEN}),
    ProspectState.UNSUBSCRIBED: frozenset({ProspectState.FORGOTTEN}),
    ProspectState.FORGOTTEN: frozenset(),
}

CONTACTABLE_STATES = frozenset(
    {ProspectState.QUEUED, ProspectState.CONTACTED, ProspectState.ENGAGED}
)


class TransitionError(Exception):
    def __init__(self, current: ProspectState, target: ProspectState) -> None:
        super().__init__(f"illegal transition {current} -> {target}")
        self.current = current
        self.target = target


def assert_transition(current: ProspectState, target: ProspectState) -> None:
    if target not in TRANSITIONS[current]:
        raise TransitionError(current, target)
