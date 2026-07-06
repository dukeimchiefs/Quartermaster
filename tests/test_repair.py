"""Tests for solver/repair.py — the call-out solver.

test_repair_realistic_call_out is the "one realistic scenario working
end-to-end" from CLAUDE.md's Development Priority #3: it runs against the
actual toy-seeded DB (db/seed.py), not fabricated data. The remaining tests
use small duck-typed stand-ins (matching tests/test_rules.py's pattern) to
exercise ranking and infeasibility paths that aren't present in the seed
data.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest

from db.models import Assignment, Block, CallHistory, Resident, Rotation, TimeOff, get_engine, get_session, init_db
from db.seed import seed
from solver.repair import CurrentSchedule, OpenShift, repair_schedule


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
    shift_type: str
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


@pytest.fixture
def seeded_schedule():
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    with get_session(engine) as session:
        seed(session)
        yield CurrentSchedule(
            assignments=session.query(Assignment).all(),
            residents=session.query(Resident).all(),
            rotations=session.query(Rotation).all(),
            blocks=session.query(Block).all(),
            time_off=session.query(TimeOff).all(),
            call_history=session.query(CallHistory).all(),
        )


def _resident_id(schedule: CurrentSchedule, name: str) -> int:
    return next(r.id for r in schedule.residents if r.name == name)


def _rotation_id(schedule: CurrentSchedule, name: str) -> int:
    return next(r.id for r in schedule.rotations if r.name == name)


def test_repair_realistic_call_out(seeded_schedule):
    """Elena Petrov (Wards, senior) calls out sick for a night-call shift.
    Farid Haddad is her only Wards-senior peer in block 1 and has no
    conflicting call history, so he should come back as the sole, top-ranked
    replacement."""
    sick_id = _resident_id(seeded_schedule, "Elena Petrov")
    farid_id = _resident_id(seeded_schedule, "Farid Haddad")
    wards_id = _rotation_id(seeded_schedule, "Wards")
    block_1_id = next(b.id for b in seeded_schedule.blocks if b.block_number == 1)

    open_shift = OpenShift(
        block_id=block_1_id,
        rotation_id=wards_id,
        role="senior",
        date=dt.date(2026, 7, 10),
        shift_type="night_call",
        hours=14.0,
    )

    proposals = repair_schedule(seeded_schedule, open_shift, sick_resident=sick_id)

    assert len(proposals) == 1
    assert proposals[0].resident_id == farid_id
    assert proposals[0].rank == 1
    assert proposals[0].projected_window_hours == 14.0


def test_repair_returns_empty_when_no_peers_available(seeded_schedule):
    """Isabel Marin is the sole senior on Clinic in block 1 — no peer exists
    to cover her, so repair should come back empty rather than erroring."""
    sick_id = _resident_id(seeded_schedule, "Isabel Marin")
    clinic_id = _rotation_id(seeded_schedule, "Clinic")
    block_1_id = next(b.id for b in seeded_schedule.blocks if b.block_number == 1)

    open_shift = OpenShift(
        block_id=block_1_id,
        rotation_id=clinic_id,
        role="senior",
        date=dt.date(2026, 7, 10),
        shift_type="night_call",
        hours=14.0,
    )

    assert repair_schedule(seeded_schedule, open_shift, sick_resident=sick_id) == []


def test_repair_excludes_candidate_who_would_blow_duty_hours():
    rotations = [FakeRotation(id=1, name="ICU", intern_capacity=2, senior_capacity=2)]
    blocks = [FakeBlock(id=1, block_number=1, start_date=dt.date(2026, 7, 1), end_date=dt.date(2026, 7, 28))]
    residents = [FakeResident(id=1, pgy=2), FakeResident(id=2, pgy=2), FakeResident(id=3, pgy=2)]
    assignments = [
        FakeAssignment(resident_id=1, block_id=1, rotation_id=1, role="senior"),  # sick
        FakeAssignment(resident_id=2, block_id=1, rotation_id=1, role="senior"),  # already near limit
        FakeAssignment(resident_id=3, block_id=1, rotation_id=1, role="senior"),  # fresh
    ]
    call_history = [FakeCallHistory(resident_id=2, date=dt.date(2026, 7, 9), shift_type="call", hours=70)]
    schedule = CurrentSchedule(
        assignments=assignments,
        residents=residents,
        rotations=rotations,
        blocks=blocks,
        time_off=[],
        call_history=call_history,
    )
    open_shift = OpenShift(
        block_id=1, rotation_id=1, role="senior", date=dt.date(2026, 7, 10), shift_type="night_call", hours=14.0
    )

    proposals = repair_schedule(schedule, open_shift, sick_resident=1)

    assert len(proposals) == 1
    assert proposals[0].resident_id == 3


def test_repair_ranks_candidates_by_least_projected_load():
    rotations = [FakeRotation(id=1, name="ICU", intern_capacity=2, senior_capacity=2)]
    blocks = [FakeBlock(id=1, block_number=1, start_date=dt.date(2026, 7, 1), end_date=dt.date(2026, 7, 28))]
    residents = [FakeResident(id=1, pgy=2), FakeResident(id=2, pgy=2), FakeResident(id=3, pgy=2)]
    assignments = [
        FakeAssignment(resident_id=1, block_id=1, rotation_id=1, role="senior"),  # sick
        FakeAssignment(resident_id=2, block_id=1, rotation_id=1, role="senior"),  # some existing load
        FakeAssignment(resident_id=3, block_id=1, rotation_id=1, role="senior"),  # no existing load
    ]
    call_history = [FakeCallHistory(resident_id=2, date=dt.date(2026, 7, 9), shift_type="call", hours=10)]
    schedule = CurrentSchedule(
        assignments=assignments,
        residents=residents,
        rotations=rotations,
        blocks=blocks,
        time_off=[],
        call_history=call_history,
    )
    open_shift = OpenShift(
        block_id=1, rotation_id=1, role="senior", date=dt.date(2026, 7, 10), shift_type="night_call", hours=14.0
    )

    proposals = repair_schedule(schedule, open_shift, sick_resident=1)

    assert [p.resident_id for p in proposals] == [3, 2]
    assert [p.rank for p in proposals] == [1, 2]
    assert proposals[0].projected_window_hours == 14.0
    assert proposals[1].projected_window_hours == 24.0
