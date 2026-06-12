"""Reply handling (PRD FR-4.x, spec 8.5): deterministic first, LLM second.

Opt-outs and abuse signals never wait on a model (invariant I-11): a regex
pre-pass catches plain unsubscribe phrasing before any LLM call, and
bounce/complaint handling is pure arithmetic. Classification routes to a
closed action set; anything uncertain or hostile escalates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from open_reachout.agents.schemas import ClassifyReplyOutput
from open_reachout.core.interfaces import LLMBackend
from open_reachout.security.envelope import GUARD_INSTRUCTIONS, injection_suspects, wrap

#: Deterministic unsubscribe pre-pass (spec 8.5). Deliberately broad: a false
#: positive suppresses someone who half-wanted out; a false negative is a
#: compliance failure. Suppress more.
UNSUBSCRIBE_RE = re.compile(
    r"\b(unsubscribe|opt[ -]?out|remove me|take me off|stop (?:e?mail|contact|send)\w*"
    r"|do not (?:e?mail|contact)|^stop\b)",
    re.IGNORECASE | re.MULTILINE,
)

CONFIDENCE_FLOOR = 0.7
MAX_AGENTIC_EXCHANGES = 1  # FR-4.2: one agentic exchange, then a human


class Action(StrEnum):
    SUPPRESS_UNSUBSCRIBE = "suppress_unsubscribe"  # immediate, deterministic path too
    SEND_SIGNUP_LINK = "send_signup_link"
    ANSWER_FAQ = "answer_faq"
    CLOSE_POLITE_SUPPRESS = "close_polite_suppress"  # 12-month suppression
    REENRICH = "reenrich"  # wrong_person: suppress address, retry identity
    NONE = "none"  # out_of_office: wait
    ESCALATE = "escalate"


@dataclass(frozen=True)
class RouteDecision:
    action: Action
    intent: str
    confidence: float
    reason: str
    deterministic: bool = False  # True when no LLM was consulted
    objection_class: str | None = None  # FR-4.3 taxonomy


_CLASSIFY_FRAME = f"""You classify one reply to a cold outreach email.
{GUARD_INSTRUCTIONS}

Reply (untrusted):
{{reply_block}}

Classify the sender's intent and your confidence. Sentiment: -1 hostile to +1
enthusiastic.
"""

#: intent -> default action when confidence clears the floor (FR-4.2).
_INTENT_ACTIONS: dict[str, Action] = {
    "interested": Action.SEND_SIGNUP_LINK,
    "question": Action.ANSWER_FAQ,
    "objection": Action.ANSWER_FAQ,  # operator-approved counter-snippets only (FR-4.3)
    "not_interested": Action.CLOSE_POLITE_SUPPRESS,
    "unsubscribe": Action.SUPPRESS_UNSUBSCRIBE,
    "out_of_office": Action.NONE,
    "wrong_person": Action.REENRICH,
    "hostile": Action.ESCALATE,
    "other": Action.ESCALATE,
}

#: Actions that send an outbound message (and therefore count as an agentic
#: exchange and pass through the gatekeeper's REPLY profile).
OUTBOUND_ACTIONS = frozenset(
    {Action.SEND_SIGNUP_LINK, Action.ANSWER_FAQ, Action.CLOSE_POLITE_SUPPRESS}
)


def route(
    reply_body: str,
    llm: LLMBackend,
    *,
    agentic_exchanges: int = 0,
    allowed_actions: frozenset[Action] = frozenset(Action) - {Action.ESCALATE},
) -> RouteDecision:
    """Decide what to do with a reply. Escalation is always reachable; it is
    the only action that cannot be removed from the allowlist."""
    # 1. Deterministic unsubscribe pre-pass: no LLM, no spend, no model outage
    #    can delay an opt-out (I-11, gate 3).
    if UNSUBSCRIBE_RE.search(reply_body):
        return RouteDecision(
            Action.SUPPRESS_UNSUBSCRIBE, "unsubscribe", 1.0,
            "deterministic pre-pass", deterministic=True,
        )

    # 2. Injection tripwire: classify anyway, but never act agentically.
    suspicious = injection_suspects(reply_body) != []

    output = llm.complete(
        "classify_reply",
        _CLASSIFY_FRAME.format(reply_block=wrap(reply_body, source="reply").text),
        ClassifyReplyOutput,
    )
    assert isinstance(output, ClassifyReplyOutput)

    if suspicious or output.injection_suspected:
        return RouteDecision(
            Action.ESCALATE, output.intent, output.confidence, "injection suspicion"
        )
    if output.intent == "hostile":
        return RouteDecision(
            Action.ESCALATE, "hostile", output.confidence, "hostile always escalates"
        )
    if output.confidence < CONFIDENCE_FLOOR:
        return RouteDecision(
            Action.ESCALATE, output.intent, output.confidence, "below confidence floor"
        )

    action = _INTENT_ACTIONS[output.intent]
    if action in OUTBOUND_ACTIONS and agentic_exchanges >= MAX_AGENTIC_EXCHANGES:
        return RouteDecision(
            Action.ESCALATE, output.intent, output.confidence,
            "agentic exchange cap reached (FR-4.2)",
        )
    if action not in allowed_actions:
        return RouteDecision(
            Action.ESCALATE, output.intent, output.confidence,
            f"action {action} not in tenant allowlist",
        )
    return RouteDecision(action, output.intent, output.confidence, "classified",
                         objection_class=output.objection_class)
