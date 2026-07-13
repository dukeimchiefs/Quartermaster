"""Tests for solver/full_schedule.py — Development Priority #8 (CLAUDE.md):
the ground-up scheduler. test_full_schedule_realistic_seeded_roster is the
"one realistic scenario working end-to-end" analogue of test_repair.py's,
run against the actual toy-seeded DB. The rest use small hand-built rosters
(matching tests/test_repair.py's Fake* pattern) to exercise capacity,
eligibility, vacation, infeasibility, preference, and fairness behavior
deterministically.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest

from db.models import Assignment, Block, Resident, Rotation, TimeOff, get_engine, get_session, init_db
from db.seed import seed
from solver.full_schedule import InfeasibleScheduleError, Roster, build_full_schedule
from solver.rules import check_all


@dataclass
class FakeResident:
    id: int
    pgy: int
    start_date: dt.date = dt.date(2020, 1, 1)
    end_date: dt.date | None = None


@dataclass
class FakeRotation:
    id: int
    name: str
    intern_capacity: int
    senior_capacity: int
    requires_pgy: int | None = None


@dataclass
class FakeBlock:
    id: int
    year: int
    block_number: int
    start_date: dt.date
    end_date: dt.date


@dataclass
class FakeTimeOff:
    resident_id: int
    start_date: dt.date
    end_date: dt.date
    type: str
    approved: bool


def _blocks(year: int, count: int) -> list[FakeBlock]:
    blocks = []
    start = dt.date(year, 1, 1)
    for n in range(1, count + 1):
        end = start + dt.timedelta(days=27)
        blocks.append(FakeBlock(id=n, year=year, block_number=n, start_date=start, end_date=end))
        start = end + dt.timedelta(days=1)
    return blocks


def test_full_schedule_realistic_seeded_roster():
    """Full year (6 blocks) for the actual toy roster: every active,
    non-leave resident-block gets exactly one eligible rotation, capacity
    and PGY eligibility are respected, and approved (but not unapproved)
    vacation blocks out an assignment."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    with get_session(engine) as session:
        seed(session)
        residents = session.query(Resident).all()
        rotations = session.query(Rotation).all()
        blocks = session.query(Block).all()
        time_off = session.query(TimeOff).all()

    roster = Roster(residents=residents, rotations=rotations, blocks=blocks, time_off=time_off)
    schedule = build_full_schedule(roster, year=2026)

    violations = check_all(
        assignments=schedule.assignments, residents=residents, rotations=rotations, blocks=blocks,
        time_off=time_off, call_history=[],
    )
    assert violations == []

    # 10 residents x 6 blocks, minus Carla Nguyen's one approved-leave block.
    assert len(schedule.assignments) == 10 * 6 - 1

    carla_id = next(r.id for r in residents if r.name == "Carla Nguyen")
    block_2_id = next(b.id for b in blocks if b.block_number == 2)
    assert not any(a.resident_id == carla_id and a.block_id == block_2_id for a in schedule.assignments)

    # Isabel Marin's time off is NOT approved -> must still get an assignment.
    isabel_id = next(r.id for r in residents if r.name == "Isabel Marin")
    block_1_id = next(b.id for b in blocks if b.block_number == 1)
    assert any(a.resident_id == isabel_id and a.block_id == block_1_id for a in schedule.assignments)

    elective_id = next(r.id for r in rotations if r.name == "Elective")
    pgy_by_id = {r.id: r.pgy for r in residents}
    assert all(
        pgy_by_id[a.resident_id] >= 2 for a in schedule.assignments if a.rotation_id == elective_id
    )


def test_full_schedule_respects_capacity():
    """3 interns, 2 eligible rotations, only enough combined capacity (2+1)
    for exactly 3 — feasible, but only if capacity is respected per block."""
    residents = [FakeResident(id=i, pgy=1) for i in range(1, 4)]
    rotations = [
        FakeRotation(id=1, name="ICU", intern_capacity=2, senior_capacity=0),
        FakeRotation(id=2, name="Wards", intern_capacity=1, senior_capacity=0),
    ]
    blocks = _blocks(2026, 1)
    roster = Roster(residents=residents, rotations=rotations, blocks=blocks, time_off=[])

    schedule = build_full_schedule(roster, year=2026)

    assert len(schedule.assignments) == 3
    counts = {1: 0, 2: 0}
    for a in schedule.assignments:
        counts[a.rotation_id] += 1
    assert counts[1] <= 2
    assert counts[2] <= 1


def test_full_schedule_raises_when_infeasible():
    residents = [FakeResident(id=1, pgy=1), FakeResident(id=2, pgy=1), FakeResident(id=3, pgy=1)]
    rotations = [FakeRotation(id=1, name="ICU", intern_capacity=1, senior_capacity=0)]
    blocks = _blocks(2026, 1)
    roster = Roster(residents=residents, rotations=rotations, blocks=blocks, time_off=[])

    with pytest.raises(InfeasibleScheduleError):
        build_full_schedule(roster, year=2026)


def test_full_schedule_excludes_pgy_ineligible_rotation():
    residents = [FakeResident(id=1, pgy=1)]
    rotations = [
        FakeRotation(id=1, name="Wards", intern_capacity=1, senior_capacity=0),
        FakeRotation(id=2, name="Elective", intern_capacity=1, senior_capacity=0, requires_pgy=2),
    ]
    blocks = _blocks(2026, 2)
    roster = Roster(residents=residents, rotations=rotations, blocks=blocks, time_off=[])

    schedule = build_full_schedule(roster, year=2026)

    assert all(a.rotation_id == 1 for a in schedule.assignments)


def test_full_schedule_skips_blocks_overlapping_approved_leave():
    residents = [FakeResident(id=1, pgy=1)]
    rotations = [FakeRotation(id=1, name="Wards", intern_capacity=1, senior_capacity=0)]
    blocks = _blocks(2026, 2)
    time_off = [FakeTimeOff(resident_id=1, start_date=blocks[0].start_date, end_date=blocks[0].end_date, type="vacation", approved=True)]
    roster = Roster(residents=residents, rotations=rotations, blocks=blocks, time_off=time_off)

    schedule = build_full_schedule(roster, year=2026)

    assert len(schedule.assignments) == 1
    assert schedule.assignments[0].block_id == blocks[1].id


def test_full_schedule_prefers_higher_scored_rotation():
    """2 residents, 2 blocks, Wards and ICU each capacity 1 (so both floor
    and ceiling force exactly one of each per block -> someone must take
    each rotation every block). Resident 1 has a strong preference for ICU;
    with a strong-enough score, the preference term should outweigh the
    fairness term's pull toward alternating, and resident 1 should get ICU
    both blocks."""
    residents = [FakeResident(id=1, pgy=1), FakeResident(id=2, pgy=1)]
    rotations = [
        FakeRotation(id=1, name="Wards", intern_capacity=1, senior_capacity=0),
        FakeRotation(id=2, name="ICU", intern_capacity=1, senior_capacity=0),
    ]
    blocks = _blocks(2026, 2)
    roster = Roster(residents=residents, rotations=rotations, blocks=blocks, time_off=[])

    schedule = build_full_schedule(roster, year=2026, preferences={1: {2: 5.0}})

    resident_1_rotations = {a.rotation_id for a in schedule.assignments if a.resident_id == 1}
    assert resident_1_rotations == {2}


def test_full_schedule_balances_load_across_residents_with_no_preferences():
    """2 residents, 4 blocks. Rotation R1 has capacity 1 (only one resident
    per block), R2 has capacity 2 (room for both). With no preferences, the
    fairness term should force an even 2/2 split of R1 exposure rather than
    one resident getting all of it."""
    residents = [FakeResident(id=1, pgy=1), FakeResident(id=2, pgy=1)]
    rotations = [
        FakeRotation(id=1, name="R1", intern_capacity=1, senior_capacity=0),
        FakeRotation(id=2, name="R2", intern_capacity=2, senior_capacity=0),
    ]
    blocks = _blocks(2026, 4)
    roster = Roster(residents=residents, rotations=rotations, blocks=blocks, time_off=[])

    schedule = build_full_schedule(roster, year=2026)

    r1_count = {1: 0, 2: 0}
    for a in schedule.assignments:
        if a.rotation_id == 1:
            r1_count[a.resident_id] += 1
    assert r1_count[1] == r1_count[2] == 2
