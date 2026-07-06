"""Tests for solver/rules.py — the highest-priority tests in this repo, per
CLAUDE.md: "a regression here is a real-world scheduling violation."

Happy-path checks run against the real toy-seeded DB (db/seed.py) to prove
the shipped seed data is itself rule-clean. Violation checks use small
duck-typed stand-ins instead of the DB, since rules.py is designed to accept
any object with the right attributes (see rules.py's module docstring).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest

from db.models import Assignment, Block, CallHistory, Resident, Rotation, TimeOff, get_engine, get_session, init_db
from db.seed import seed
from solver.rules import (
    check_all,
    check_duty_hours,
    check_no_double_coverage,
    check_no_same_day_double_shift,
    check_rotation_requirements,
    check_vacation_respect,
    rolling_window_hours,
)


@dataclass
class FakeAssignment:
    resident_id: int
    block_id: int
    rotation_id: int
    role: str


@dataclass
class FakeCallHistory:
    resident_id: int
    date: dt.date
    hours: float


@dataclass
class FakeRotation:
    id: int
    name: str
    intern_capacity: int
    senior_capacity: int
    requires_pgy: int | None = None


@dataclass
class FakeResident:
    id: int
    pgy: int


@dataclass
class FakeBlock:
    id: int
    block_number: int
    start_date: dt.date
    end_date: dt.date


@dataclass
class FakeTimeOff:
    resident_id: int
    start_date: dt.date
    end_date: dt.date
    approved: bool


@pytest.fixture
def seeded_session():
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    with get_session(engine) as session:
        seed(session)
        yield session


def test_seed_data_has_no_violations(seeded_session):
    violations = check_all(
        assignments=seeded_session.query(Assignment).all(),
        residents=seeded_session.query(Resident).all(),
        rotations=seeded_session.query(Rotation).all(),
        blocks=seeded_session.query(Block).all(),
        time_off=seeded_session.query(TimeOff).all(),
        call_history=seeded_session.query(CallHistory).all(),
    )
    assert violations == []


def test_duty_hours_flags_shift_over_limit():
    history = [FakeCallHistory(resident_id=1, date=dt.date(2026, 7, 1), hours=30)]
    violations = check_duty_hours(history)
    assert len(violations) == 1
    assert violations[0].rule == "duty_hours"
    assert violations[0].resident_id == 1


def test_duty_hours_flags_rolling_week_over_limit():
    history = [
        FakeCallHistory(resident_id=1, date=dt.date(2026, 7, 1) + dt.timedelta(days=i), hours=15)
        for i in range(6)
    ]  # 6 days x 15h = 90h within a 7-day window, over the 80h/week default
    violations = check_duty_hours(history)
    assert len(violations) == 1
    assert violations[0].resident_id == 1


def test_duty_hours_ok_within_limits():
    history = [FakeCallHistory(resident_id=1, date=dt.date(2026, 7, 1), hours=12)]
    assert check_duty_hours(history) == []


def test_no_double_coverage_flags_duplicate_block_assignment():
    assignments = [
        FakeAssignment(resident_id=1, block_id=1, rotation_id=1, role="intern"),
        FakeAssignment(resident_id=1, block_id=1, rotation_id=2, role="intern"),
    ]
    violations = check_no_double_coverage(assignments)
    assert len(violations) == 1
    assert violations[0].resident_id == 1
    assert violations[0].block_id == 1


def test_no_double_coverage_ok_across_different_blocks():
    assignments = [
        FakeAssignment(resident_id=1, block_id=1, rotation_id=1, role="intern"),
        FakeAssignment(resident_id=1, block_id=2, rotation_id=1, role="intern"),
    ]
    assert check_no_double_coverage(assignments) == []


def test_rolling_window_hours_sums_within_window():
    history = [
        FakeCallHistory(resident_id=1, date=dt.date(2026, 7, 1), hours=10),
        FakeCallHistory(resident_id=1, date=dt.date(2026, 7, 5), hours=8),
        FakeCallHistory(resident_id=1, date=dt.date(2026, 6, 1), hours=100),  # outside window
    ]
    assert rolling_window_hours(1, history, dt.date(2026, 7, 5), window_days=7) == 18


def test_no_same_day_double_shift_flags_duplicate_date():
    history = [
        FakeCallHistory(resident_id=1, date=dt.date(2026, 7, 1), hours=12),
        FakeCallHistory(resident_id=1, date=dt.date(2026, 7, 1), hours=8),
    ]
    violations = check_no_same_day_double_shift(history)
    assert len(violations) == 1
    assert violations[0].resident_id == 1


def test_no_same_day_double_shift_ok_across_different_dates():
    history = [
        FakeCallHistory(resident_id=1, date=dt.date(2026, 7, 1), hours=12),
        FakeCallHistory(resident_id=1, date=dt.date(2026, 7, 2), hours=8),
    ]
    assert check_no_same_day_double_shift(history) == []


def test_rotation_requirements_flags_capacity_overflow():
    rotations = [FakeRotation(id=1, name="ICU", intern_capacity=1, senior_capacity=1)]
    residents = [FakeResident(id=1, pgy=1), FakeResident(id=2, pgy=1)]
    assignments = [
        FakeAssignment(resident_id=1, block_id=1, rotation_id=1, role="intern"),
        FakeAssignment(resident_id=2, block_id=1, rotation_id=1, role="intern"),
    ]
    violations = check_rotation_requirements(assignments, rotations, residents)
    assert len(violations) == 1
    assert violations[0].rule == "rotation_requirements"


def test_rotation_requirements_flags_pgy_ineligibility():
    rotations = [FakeRotation(id=1, name="Elective", intern_capacity=2, senior_capacity=2, requires_pgy=2)]
    residents = [FakeResident(id=1, pgy=1)]
    assignments = [FakeAssignment(resident_id=1, block_id=1, rotation_id=1, role="intern")]
    violations = check_rotation_requirements(assignments, rotations, residents)
    assert len(violations) == 1
    assert violations[0].resident_id == 1


def test_rotation_requirements_ok_within_capacity_and_eligible():
    rotations = [FakeRotation(id=1, name="Elective", intern_capacity=2, senior_capacity=2, requires_pgy=2)]
    residents = [FakeResident(id=1, pgy=3)]
    assignments = [FakeAssignment(resident_id=1, block_id=1, rotation_id=1, role="senior")]
    assert check_rotation_requirements(assignments, rotations, residents) == []


def test_vacation_respect_flags_overlap():
    blocks = [FakeBlock(id=1, block_number=1, start_date=dt.date(2026, 7, 1), end_date=dt.date(2026, 7, 28))]
    time_off = [
        FakeTimeOff(
            resident_id=1, start_date=dt.date(2026, 7, 10), end_date=dt.date(2026, 7, 15), approved=True
        )
    ]
    assignments = [FakeAssignment(resident_id=1, block_id=1, rotation_id=1, role="intern")]
    violations = check_vacation_respect(assignments, time_off, blocks)
    assert len(violations) == 1
    assert violations[0].rule == "vacation_respect"


def test_vacation_respect_ignores_unapproved_time_off():
    blocks = [FakeBlock(id=1, block_number=1, start_date=dt.date(2026, 7, 1), end_date=dt.date(2026, 7, 28))]
    time_off = [
        FakeTimeOff(
            resident_id=1, start_date=dt.date(2026, 7, 10), end_date=dt.date(2026, 7, 15), approved=False
        )
    ]
    assignments = [FakeAssignment(resident_id=1, block_id=1, rotation_id=1, role="intern")]
    assert check_vacation_respect(assignments, time_off, blocks) == []


def test_vacation_respect_ok_no_overlap():
    blocks = [FakeBlock(id=1, block_number=1, start_date=dt.date(2026, 7, 1), end_date=dt.date(2026, 7, 28))]
    time_off = [
        FakeTimeOff(
            resident_id=1, start_date=dt.date(2026, 8, 1), end_date=dt.date(2026, 8, 5), approved=True
        )
    ]
    assignments = [FakeAssignment(resident_id=1, block_id=1, rotation_id=1, role="intern")]
    assert check_vacation_respect(assignments, time_off, blocks) == []
