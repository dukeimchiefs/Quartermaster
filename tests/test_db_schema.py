"""End-to-end checks for db/schema.sql, db/models.py, and db/seed.py.

Runs entirely against sqlite:///:memory: — never touches a real database or
Resident_Schedules/.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from db.models import (
    Assignment,
    AuditLog,
    Block,
    CallHistory,
    Resident,
    Rotation,
    Rule,
    Swap,
    TimeOff,
    get_engine,
    get_session,
    init_db,
)
from db.seed import BLOCK_1_ASSIGNMENTS, CALL_HISTORY, RESIDENTS, ROTATIONS, RULES, TIME_OFF, seed

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def test_schema_sql_is_valid_ddl():
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA_PATH.read_text())
    finally:
        conn.close()


@pytest.fixture
def session():
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    with get_session(engine) as s:
        seed(s)
        yield s


def test_seed_row_counts(session):
    assert session.query(Resident).count() == len(RESIDENTS)
    assert session.query(Rotation).count() == len(ROTATIONS)
    assert session.query(Block).count() == 6
    assert session.query(Assignment).count() == len(BLOCK_1_ASSIGNMENTS)
    assert session.query(TimeOff).count() == len(TIME_OFF)
    assert session.query(CallHistory).count() == len(CALL_HISTORY)
    assert session.query(Rule).count() == len(RULES)
    assert session.query(Swap).count() == 0
    assert session.query(AuditLog).count() == 1


def test_capacity_boundaries_respected(session):
    wards = session.query(Rotation).filter_by(name="Wards").one()
    icu = session.query(Rotation).filter_by(name="ICU").one()

    wards_interns = (
        session.query(Assignment)
        .filter_by(rotation_id=wards.id, role="intern")
        .count()
    )
    wards_seniors = (
        session.query(Assignment)
        .filter_by(rotation_id=wards.id, role="senior")
        .count()
    )
    icu_seniors = (
        session.query(Assignment)
        .filter_by(rotation_id=icu.id, role="senior")
        .count()
    )

    assert wards_interns == wards.intern_capacity == 4
    assert wards_seniors == wards.senior_capacity == 2
    assert icu_seniors == icu.senior_capacity == 2

    for rotation in session.query(Rotation).all():
        interns = (
            session.query(Assignment)
            .filter_by(rotation_id=rotation.id, role="intern")
            .count()
        )
        seniors = (
            session.query(Assignment)
            .filter_by(rotation_id=rotation.id, role="senior")
            .count()
        )
        assert interns <= rotation.intern_capacity
        assert seniors <= rotation.senior_capacity


def test_foreign_key_violation_raises(session):
    bogus_assignment = Assignment(
        resident_id=99999,
        block_id=session.query(Block).first().id,
        rotation_id=session.query(Rotation).first().id,
        role="intern",
    )
    session.add(bogus_assignment)
    with pytest.raises(IntegrityError):
        session.commit()
