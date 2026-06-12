"""Sender-profile research (PRD FR-0.7, spec 8.10).

Personalization has two sides. The system researches *the sender* — the
operator's own `about_us` block plus any operator-supplied bio/site text —
into credibility facts shaped for outreach ("8 years booking rooms in
Austin"). Output always lands `proposed`; one-time human approval elevates
the facts to trusted-class `{{sender.*}}` variables. This is the only path
by which researched content becomes trusted, and it requires a human.
Re-research files a new proposed profile; an approved one is never silently
mutated.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout.core.config import TenantConfig
from open_reachout.core.escalations import HUMAN_ACTOR_PREFIX
from open_reachout.core.interfaces import LLMBackend


class SenderFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(pattern=r"^[a-z0-9_]+$")
    fact: str = Field(min_length=5)
    source_url: str = ""


class SenderProfileOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: list[SenderFact]
    injection_suspected: bool = False


RESEARCH_FRAME = """Extract SENDER credibility facts for outreach
personalization: who is reaching out, what genuine experience/context they
bring, what shared-context hooks exist with the audience. Facts must be
verbatim-supported by the material below — never embellish, never invent.
3-6 short facts, each with a snake_case slug.

About the sender (operator-authored):
{about_us}

Additional material (operator-supplied bio/site text, may be empty):
{site_text}
"""


def propose(
    conn: Connection, llm: LLMBackend, config: TenantConfig, site_text: str = ""
) -> str:
    """Research → a `proposed` profile row. Returns its id."""
    about = config.brief.about_us
    prompt = RESEARCH_FRAME.format(
        about_us=json.dumps(
            {"name": about.name, "what_we_do": about.what_we_do,
             "sender": about.identity.sender, "links": about.links},
            indent=2,
        ),
        site_text=site_text or "(none)",
    )
    result = llm.complete("sender_research", prompt, SenderProfileOutput)
    assert isinstance(result, SenderProfileOutput)
    row = conn.execute(
        text(
            """
            INSERT INTO sender_profiles (tenant, facts)
            VALUES (:t, CAST(:f AS jsonb)) RETURNING id
            """
        ),
        {"t": config.tenant,
         "f": json.dumps([f.model_dump() for f in result.facts])},
    ).fetchone()
    assert row is not None
    return str(row[0])


def approve(conn: Connection, profile_id: str, *, actor: str) -> bool:
    """One-time human approval: facts become trusted {{sender.*}} variables.
    Any previously approved profile is superseded (one active profile)."""
    if not actor.startswith(HUMAN_ACTOR_PREFIX):
        raise PermissionError(f"sender-profile approval requires a human, got {actor!r}")
    row = conn.execute(
        text(
            """
            UPDATE sender_profiles SET status='approved', approved_by=:a,
                resolved_at=now()
            WHERE id = CAST(:i AS uuid) AND status = 'proposed'
            RETURNING tenant
            """
        ),
        {"i": profile_id, "a": actor},
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        text(
            """
            UPDATE sender_profiles SET status='superseded', resolved_at=now()
            WHERE tenant = :t AND status = 'approved' AND id != CAST(:i AS uuid)
            """
        ),
        {"t": row[0], "i": profile_id},
    )
    conn.execute(
        text(
            """
            INSERT INTO audit_events (subject_type, subject_id, event, payload, actor)
            VALUES ('sender_profile', :i, 'approved', '{}'::jsonb, :a)
            """
        ),
        {"i": profile_id, "a": actor},
    )
    return True


def approved_facts(conn: Connection, tenant: str) -> dict[str, str]:
    """slug -> fact for the tenant's active approved profile ({} if none)."""
    row = conn.execute(
        text(
            """
            SELECT facts FROM sender_profiles
            WHERE tenant = :t AND status = 'approved'
            ORDER BY resolved_at DESC LIMIT 1
            """
        ),
        {"t": tenant},
    ).fetchone()
    if row is None:
        return {}
    return {f["slug"]: f["fact"] for f in row[0]}


def list_proposed(
    conn: Connection, tenant: str | None = None
) -> list[tuple[str, str, list[dict[str, str]]]]:
    rows = conn.execute(
        text(
            """
            SELECT id, tenant, facts FROM sender_profiles
            WHERE status = 'proposed' AND (CAST(:t AS text) IS NULL OR tenant = :t)
            ORDER BY created_at
            """
        ),
        {"t": tenant},
    ).fetchall()
    return [(str(r[0]), r[1], r[2]) for r in rows]
