"""Postgres GateStore: the claim transaction (spec 7.1).

Binds the gatekeeper's pinned gate order to row locks. The caller owns the
transaction:

    with engine.begin() as conn:
        result = claim(PgGateStore(conn), draft_touch)

Every method here runs inside that single transaction, so a successful claim
commits gates + counters + touch + trace atomically, and any refusal or error
rolls all of it back (fail closed).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from open_reachout import DEFAULT_ANNUAL_TOUCH_CAP, DEFAULT_MIN_CAMPAIGN_GAP_DAYS
from open_reachout.core import control, suppression
from open_reachout.core.gatekeeper import DraftTouch, GateProfile


def _month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class PgGateStore:
    """Implements core.gatekeeper.GateStore over one open transaction."""

    def __init__(
        self,
        conn: Connection,
        *,
        min_campaign_gap_days: int = DEFAULT_MIN_CAMPAIGN_GAP_DAYS,
        annual_touch_cap: int = DEFAULT_ANNUAL_TOUCH_CAP,
    ) -> None:
        self.conn = conn
        self.min_gap_days = min_campaign_gap_days
        self.annual_cap = annual_touch_cap

    # -- gate 1: halt (I-2) ---------------------------------------------------
    def halted_scopes(self, tenant: str) -> list[str]:
        return control.halted_scopes(self.conn, tenant)

    # -- gate 3: suppression (I-3) --------------------------------------------
    def is_suppressed(self, email_canonical: str, tenant: str) -> bool:
        return suppression.is_suppressed(self.conn, email_canonical, tenant)

    # -- gate 4: entity frequency (I-7), row-locked ----------------------------
    def frequency_ok(self, entity_id: str, tenant: str) -> bool:
        row = self.conn.execute(
            text(
                """
                SELECT active_sequence_touch_id, last_campaign_contact_at, touches_12mo
                FROM entities WHERE id = CAST(:e AS uuid) FOR UPDATE
                """
            ),
            {"e": entity_id},
        ).fetchone()
        if row is None:
            return False  # unknown entity: fail closed
        active_seq, last_contact, touches_12mo = row
        if active_seq is not None:
            return False  # at most one active sequence (FR-7.3)
        if touches_12mo >= self.annual_cap:
            return False
        if last_contact is not None:
            gap = datetime.now(UTC) - last_contact
            if gap.days < self.min_gap_days:
                return False
        return True

    # -- gate 5: volume budgets (I-8), guarded atomic increments ---------------
    def try_consume_budget(self, tenant: str, touch: DraftTouch) -> bool:
        for scope_type, scope_id in (
            ("tenant_month", tenant),
            ("cohort_month", touch.cohort_id),
        ):
            updated = self.conn.execute(
                text(
                    """
                    UPDATE counters SET used = used + 1
                    WHERE scope_type = :st AND scope_id = :si AND period = :p
                      AND used < cap
                    """
                ),
                {"st": scope_type, "si": scope_id, "p": _month()},
            ).rowcount
            if not updated:
                if scope_type == "cohort_month":  # roll back the tenant increment
                    self._adjust("tenant_month", tenant, -1, "cohort cap refusal")
                return False
        return True

    def release_budget(self, tenant: str, touch: DraftTouch) -> None:
        self._adjust("tenant_month", tenant, -1, "downstream refusal")
        self._adjust("cohort_month", touch.cohort_id, -1, "downstream refusal")

    def _adjust(self, scope_type: str, scope_id: str, delta: int, why: str) -> None:
        # Counters are never silently edited (spec 7.4): every compensation
        # leaves an audit row.
        self.conn.execute(
            text(
                """
                UPDATE counters SET used = GREATEST(used + :d, 0)
                WHERE scope_type = :st AND scope_id = :si AND period = :p
                """
            ),
            {"d": delta, "st": scope_type, "si": scope_id, "p": _month()},
        )
        self.conn.execute(
            text(
                """
                INSERT INTO audit_events (subject_type, subject_id, event, payload, actor)
                VALUES ('counter', :si, 'adjustment',
                        CAST(:pl AS jsonb), 'system:gatekeeper')
                """
            ),
            {"si": f"{scope_type}:{scope_id}", "pl": json.dumps({"delta": delta, "why": why})},
        )

    # -- gate 6: mailbox capacity ----------------------------------------------
    def pick_mailbox(self, tenant: str) -> str | None:
        # Ensure today's counter rows exist, then pick the least-used healthy
        # inbox under its cap, locking it against concurrent claimers.
        self.conn.execute(
            text(
                """
                INSERT INTO counters (scope_type, scope_id, period, used, cap)
                SELECT 'mailbox_day', mailbox, :p, 0, daily_cap
                FROM mailboxes WHERE tenant = :t
                ON CONFLICT DO NOTHING
                """
            ),
            {"p": _today(), "t": tenant},
        )
        row = self.conn.execute(
            text(
                """
                SELECT c.scope_id FROM counters c
                JOIN mailboxes m ON m.mailbox = c.scope_id AND m.tenant = :t
                WHERE c.scope_type = 'mailbox_day' AND c.period = :p
                  AND c.used < c.cap AND m.warmup_complete
                ORDER BY c.used ASC
                FOR UPDATE OF c SKIP LOCKED
                LIMIT 1
                """
            ),
            {"t": tenant, "p": _today()},
        ).fetchone()
        if row is None:
            return None
        mailbox = str(row[0])
        self.conn.execute(
            text(
                """
                UPDATE counters SET used = used + 1
                WHERE scope_type = 'mailbox_day' AND scope_id = :m AND period = :p
                """
            ),
            {"m": mailbox, "p": _today()},
        )
        return mailbox

    # -- gate 7: verification confidence (FR-2.6) -------------------------------
    def confidence_sendable(self, email_canonical: str, tenant: str) -> bool:
        row = self.conn.execute(
            text(
                """
                SELECT 1 FROM prospects p
                JOIN tenants t ON t.id = p.tenant_id
                WHERE p.email_canonical = :e AND t.slug = :t
                  AND p.email_confidence = 'verified'
                LIMIT 1
                """
            ),
            {"e": email_canonical, "t": tenant},
        ).fetchone()
        return row is not None

    # -- gate 8: persist ---------------------------------------------------------
    def persist_claim(
        self, touch: DraftTouch, mailbox: str, gate_results: dict[str, str]
    ) -> None:
        claimed = self.conn.execute(
            text(
                """
                UPDATE touches SET status = 'claimed', claimed_at = now(), mailbox = :m
                WHERE id = CAST(:i AS uuid) AND status = 'drafted'
                """
            ),
            {"i": touch.touch_id, "m": mailbox},
        ).rowcount
        if not claimed:
            raise RuntimeError(f"touch {touch.touch_id} not in 'drafted' state")
        if touch.profile is GateProfile.COLD:
            self.conn.execute(
                text(
                    """
                    UPDATE entities SET
                        active_sequence_touch_id = CAST(:i AS uuid),
                        last_campaign_contact_at = now(),
                        touches_12mo = touches_12mo + 1
                    WHERE id = CAST(:e AS uuid)
                    """
                ),
                {"i": touch.touch_id, "e": touch.entity_id},
            )
        self.conn.execute(
            text(
                """
                INSERT INTO decision_traces (touch_id, gate_results)
                VALUES (CAST(:i AS uuid), CAST(:g AS jsonb))
                ON CONFLICT (touch_id)
                DO UPDATE SET gate_results = EXCLUDED.gate_results
                """
            ),
            {"i": touch.touch_id, "g": json.dumps(gate_results)},
        )
        self.conn.execute(
            text(
                """
                INSERT INTO audit_events (subject_type, subject_id, event, actor)
                VALUES ('touch', :i, 'claimed', 'system:gatekeeper')
                """
            ),
            {"i": touch.touch_id},
        )

    # -- dispatch reissue (spec 7.4): re-verify the absolute gates at load -----
    def load_claimed(self, touch_id: str):
        from open_reachout.core.gatekeeper import ClaimedSnapshot

        row = self.conn.execute(
            text(
                """
                SELECT t.slug, tc.mailbox, tc.content_hash, p.email_canonical
                FROM touches tc
                JOIN prospects p ON p.id = tc.prospect_id
                JOIN tenants t ON t.id = p.tenant_id
                WHERE tc.id = CAST(:i AS uuid) AND tc.status = 'claimed'
                  AND tc.mailbox IS NOT NULL
                """
            ),
            {"i": touch_id},
        ).fetchone()
        if row is None:
            return None
        tenant, mailbox, content_hash, email = row
        if self.halted_scopes(tenant):
            return None
        if email is None or self.is_suppressed(email, tenant):
            return None
        return ClaimedSnapshot(
            touch_id=touch_id, tenant=tenant, mailbox=mailbox,
            content_sha256=content_hash, recipient=email,
        )
