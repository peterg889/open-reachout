"""Funnel metrics (the UI's top-level numbers): how many were reached, who
responded, and where prospects abandon the flow.

Every prospect state maps to the furthest stage it passed, and exit states
record WHERE the flow lost them — so "abandonment" is a first-class number,
not an inference.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: state -> furthest funnel stage reached (1-6). Forgotten is excluded.
STAGE_OF: dict[str, int] = {
    "discovered": 1, "unenrichable": 1,
    "enriched": 2, "disqualified": 2,
    "qualified": 3, "queued": 3,
    "contacted": 4, "bounced": 4, "no_response": 4,
    "declined": 4, "unsubscribed": 4,
    "engaged": 5,
    "converted": 6,
}

STAGES = ("discovered", "enriched", "qualified", "contacted", "engaged", "converted")

#: exit state -> human-readable abandonment point.
EXIT_LABEL: dict[str, str] = {
    "unenrichable": "enrichment (no verified email)",
    "disqualified": "qualification (didn't match persona)",
    "bounced": "delivery (bounced)",
    "unsubscribed": "after contact (opted out)",
    "declined": "after contact (not interested)",
    "no_response": "after contact (no response)",
}


@dataclass(frozen=True)
class Funnel:
    reached: list[tuple[str, int]]  # (stage, prospects who got at least this far)
    exits: dict[str, int]  # abandonment label -> count
    replies: int
    positive_replies: int
    converted: int
    in_conversation: int  # engaged, not yet converted

    @property
    def contacted(self) -> int:
        return dict(self.reached).get("contacted", 0)

    @property
    def reply_rate(self) -> float:
        return self.replies / self.contacted if self.contacted else 0.0

    @property
    def conversion_rate(self) -> float:
        return self.converted / self.contacted if self.contacted else 0.0


def funnel(conn: Connection, tenant: str, cohort_id: str | None = None) -> Funnel:
    rows = conn.execute(
        text(
            """
            SELECT p.state, count(*) FROM prospects p
            JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = :t AND p.state != 'forgotten'
              AND (CAST(:c AS text) IS NULL OR p.cohort_id = :c)
            GROUP BY p.state
            """
        ),
        {"t": tenant, "c": cohort_id},
    ).fetchall()
    by_state = dict(rows)

    reached = [
        (stage, sum(n for s, n in by_state.items() if STAGE_OF.get(s, 0) >= i))
        for i, stage in enumerate(STAGES, start=1)
    ]
    exits: dict[str, int] = {}
    for state, label in EXIT_LABEL.items():
        if by_state.get(state):
            exits[label] = exits.get(label, 0) + by_state[state]

    reply_rows = conn.execute(
        text(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE r.intent = 'interested')
            FROM replies r
            JOIN prospects p ON p.id = r.prospect_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = :t
              AND (CAST(:c AS text) IS NULL OR p.cohort_id = :c)
            """
        ),
        {"t": tenant, "c": cohort_id},
    ).fetchone()
    replies, positive = (reply_rows or (0, 0))

    return Funnel(
        reached=reached,
        exits=exits,
        replies=replies or 0,
        positive_replies=positive or 0,
        converted=by_state.get("converted", 0),
        in_conversation=by_state.get("engaged", 0),
    )


@dataclass(frozen=True)
class StrategyStats:
    variant_id: str
    attributes: dict[str, str]
    trials: int
    successes: int
    bounces: int
    complaints: int
    paused: bool

    @property
    def success_rate(self) -> float:
        return self.successes / self.trials if self.trials else 0.0


def strategies(conn: Connection, tenant: str) -> list[StrategyStats]:
    rows = conn.execute(
        text(
            """
            SELECT variant_id, attributes, trials, successes, bounces, complaints, paused
            FROM variant_stats WHERE tenant = :t ORDER BY successes DESC, trials DESC
            """
        ),
        {"t": tenant},
    ).fetchall()
    return [
        StrategyStats(r[0], r[1] or {}, r[2], r[3], r[4], r[5], r[6]) for r in rows
    ]


@dataclass(frozen=True)
class MemberRow:
    prospect_id: str
    display_name: str
    state: str
    email_confidence: str | None
    touches: int
    replies: int


def members(conn: Connection, tenant: str, cohort_id: str) -> list[MemberRow]:
    rows = conn.execute(
        text(
            """
            SELECT p.id, coalesce(e.display_name, '(forgotten)'), p.state,
                   p.email_confidence,
                   (SELECT count(*) FROM touches tc WHERE tc.prospect_id = p.id
                      AND tc.status IN ('dispatched','sent')),
                   (SELECT count(*) FROM replies r WHERE r.prospect_id = p.id)
            FROM prospects p
            JOIN entities e ON e.id = p.entity_id
            JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = :t AND p.cohort_id = :c
            ORDER BY p.created_at
            """
        ),
        {"t": tenant, "c": cohort_id},
    ).fetchall()
    return [MemberRow(str(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows]


@dataclass(frozen=True)
class CohortSummary:
    cohort_id: str
    persona_id: str
    members: int
    contacted: int
    replies: int
    converted: int


def cohorts(conn: Connection, tenant: str) -> list[CohortSummary]:
    rows = conn.execute(
        text(
            """
            SELECT p.cohort_id, max(p.persona_id), count(*),
                   count(*) FILTER (WHERE p.state NOT IN
                       ('discovered','enriched','qualified','queued',
                        'disqualified','unenrichable')),
                   (SELECT count(*) FROM replies r JOIN prospects p2
                      ON p2.id = r.prospect_id WHERE p2.cohort_id = p.cohort_id),
                   count(*) FILTER (WHERE p.state = 'converted')
            FROM prospects p
            JOIN tenants t ON t.id = p.tenant_id
            WHERE t.slug = :t AND p.state != 'forgotten'
            GROUP BY p.cohort_id ORDER BY p.cohort_id
            """
        ),
        {"t": tenant},
    ).fetchall()
    return [CohortSummary(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]


@dataclass(frozen=True)
class ConversationItem:
    direction: str  # out|in
    when: object
    subject: str | None
    body: str | None
    status: str | None  # touch status or reply intent
    variant_id: str | None = None


@dataclass(frozen=True)
class MemberDetail:
    prospect_id: str
    display_name: str
    state: str
    cohort_id: str
    persona_id: str
    source_adapter: str
    data_basis: str
    evidence: list[tuple[str, str, str, object]]  # (type, content, source_url, observed_at)
    conversation: list[ConversationItem] = field(default_factory=list)


def member_detail(conn: Connection, prospect_id: str) -> MemberDetail | None:
    row = conn.execute(
        text(
            """
            SELECT coalesce(e.display_name, '(forgotten)'), p.state, p.cohort_id,
                   p.persona_id, p.source_adapter, p.data_basis
            FROM prospects p JOIN entities e ON e.id = p.entity_id
            WHERE p.id = CAST(:i AS uuid)
            """
        ),
        {"i": prospect_id},
    ).fetchone()
    if row is None:
        return None
    evidence = conn.execute(
        text(
            """
            SELECT fact_type, content, source_url, observed_at FROM evidence_facts
            WHERE prospect_id = CAST(:i AS uuid) ORDER BY observed_at
            """
        ),
        {"i": prospect_id},
    ).fetchall()
    convo: list[ConversationItem] = []
    for t in conn.execute(
        text(
            """
            SELECT coalesce(sent_at, claimed_at), subject, body, status, variant_id
            FROM touches WHERE prospect_id = CAST(:i AS uuid)
            ORDER BY coalesce(sent_at, claimed_at) NULLS LAST
            """
        ),
        {"i": prospect_id},
    ).fetchall():
        convo.append(ConversationItem("out", t[0], t[1], t[2], t[3], t[4]))
    for r in conn.execute(
        text(
            """
            SELECT received_at, body, intent FROM replies
            WHERE prospect_id = CAST(:i AS uuid) ORDER BY received_at
            """
        ),
        {"i": prospect_id},
    ).fetchall():
        convo.append(ConversationItem("in", r[0], None, r[1], r[2]))
    convo.sort(key=lambda c: (c.when is None, c.when))
    return MemberDetail(
        prospect_id=prospect_id, display_name=row[0], state=row[1], cohort_id=row[2],
        persona_id=row[3], source_adapter=row[4], data_basis=row[5],
        evidence=[(e[0], e[1] if isinstance(e[1], str) else str(e[1]), e[2], e[3])
                  for e in evidence],
        conversation=convo,
    )
