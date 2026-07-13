"""Append-only audit logger.

Every schedule change the system proposes or commits must be written here
before returning to the caller — timestamp, actor, and reason. This is the
only sink permitted to contain PII in production mode. See CLAUDE.md's
Non-Negotiable Privacy & Governance Constraints.

Development Priority #7 (CLAUDE.md). Append-only means exactly that: this
module only ever INSERTs an AuditLog row. Nothing here updates or deletes
one, and no other code in this repo should either.
"""

from __future__ import annotations

from sqlalchemy import Engine

from db.models import AuditLog, get_engine, get_session


def record(
    actor: str,
    action: str,
    reason: str | None = None,
    details: str | None = None,
    *,
    engine: Engine | None = None,
) -> AuditLog:
    """Write one audit_log row and commit it immediately, in its own short
    session, so the entry is durable even if the caller's own DB work in a
    separate session later fails — the audit trail must survive regardless
    of whether the thing it's recording ultimately succeeds.

    `engine` defaults to the production DB (db.models.get_engine()); tests
    pass an in-memory engine instead.
    """
    engine = engine or get_engine()
    with get_session(engine) as session:
        entry = AuditLog(actor=actor, action=action, reason=reason, details=details)
        session.add(entry)
        session.commit()
        session.refresh(entry)
        return entry
