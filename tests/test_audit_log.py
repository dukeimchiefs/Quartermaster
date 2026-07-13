"""Tests for audit/log.py — Development Priority #7 (CLAUDE.md): every
proposed or committed schedule change must be written to an append-only
log with timestamp, actor, and reason.
"""

from __future__ import annotations

from db.models import AuditLog, get_engine, get_session, init_db
from audit.log import record


def _memory_engine():
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    return engine


def test_record_writes_a_row_with_timestamp_actor_and_reason():
    engine = _memory_engine()

    entry = record("dr_smith", "propose_swap", "call-out coverage search", details='{"foo": 1}', engine=engine)

    assert entry.id is not None
    assert entry.actor == "dr_smith"
    assert entry.action == "propose_swap"
    assert entry.reason == "call-out coverage search"
    assert entry.details == '{"foo": 1}'
    assert entry.timestamp is not None

    with get_session(engine) as session:
        rows = session.query(AuditLog).all()
        assert len(rows) == 1
        assert rows[0].actor == "dr_smith"


def test_record_is_append_only_across_multiple_calls():
    engine = _memory_engine()

    record("dr_smith", "propose_swap", "first", engine=engine)
    record("dr_jones", "propose_swap", "second", engine=engine)

    with get_session(engine) as session:
        rows = session.query(AuditLog).order_by(AuditLog.id).all()
        assert [r.actor for r in rows] == ["dr_smith", "dr_jones"]
        assert [r.reason for r in rows] == ["first", "second"]


def test_record_reason_and_details_are_optional():
    engine = _memory_engine()

    entry = record("dr_smith", "propose_swap", engine=engine)

    assert entry.reason is None
    assert entry.details is None
