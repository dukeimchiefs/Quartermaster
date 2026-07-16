"""Tests for db/remove_toy_seed_data.py — runs entirely against
sqlite:///:memory:, never touches a real database or Resident_Schedules/.
"""

from __future__ import annotations

import datetime as dt

import pytest

from db.models import Assignment, CallHistory, Resident, Rotation, Swap, TimeOff, get_engine, get_session, init_db
from db.remove_toy_seed_data import TOY_NAMES, remove_toy_seed_data
from db.seed import seed


@pytest.fixture
def session():
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    with get_session(engine) as s:
        seed(s)
        yield s


def test_removes_all_toy_residents_and_their_rows(session):
    summary = remove_toy_seed_data(session)

    assert summary["residents"] == len(TOY_NAMES)
    assert summary["assignments"] == 10  # db/seed.py's BLOCK_1_ASSIGNMENTS
    assert summary["time_off"] == 2
    assert summary["call_history"] == 2

    assert session.query(Resident).filter(Resident.name.in_(TOY_NAMES)).count() == 0
    assert session.query(Assignment).count() == 0
    assert session.query(TimeOff).count() == 0
    assert session.query(CallHistory).count() == 0
    # Rotations are shared/global, not resident-specific — untouched.
    assert session.query(Rotation).count() > 0


def test_is_idempotent_on_rerun(session):
    first = remove_toy_seed_data(session)
    second = remove_toy_seed_data(session)

    assert first["residents"] == len(TOY_NAMES)
    assert second == {"residents": 0, "assignments": 0, "time_off": 0, "call_history": 0}


def test_never_touches_a_real_resident_with_a_different_name(session):
    real_resident = Resident(name="Choi, Christopher", pgy=2, start_date=dt.date(2026, 7, 1))
    session.add(real_resident)
    session.commit()

    remove_toy_seed_data(session)

    assert session.query(Resident).filter_by(name="Choi, Christopher").one_or_none() is not None


def test_rolls_back_if_a_swap_references_a_toy_assignment(session):
    toy_assignment = session.query(Assignment).first()
    session.add(Swap(original_assignment_id=toy_assignment.id, reason="test"))
    session.commit()

    with pytest.raises(Exception):
        remove_toy_seed_data(session)
