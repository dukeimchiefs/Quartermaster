"""Tests for solver/warm_start.py — Development Priority #11 (CLAUDE.md):
mid-cycle revisions that reuse full_schedule.py's model with a deviation
penalty. test_warm_start_realistic_mid_cycle_leave is the "one realistic
scenario" analogue of the other solver test files, run against the actual
seeded roster; the rest use small hand-built rosters to make the deviation
penalty's effect deterministically checkable.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from db.models import Block, Resident, Rotation, TimeOff, get_engine, get_session, init_db
from db.seed import seed
from solver.full_schedule import ProposedAssignment, Roster, build_full_schedule
from solver.rules import check_all
from solver.warm_start import CurrentFullSchedule, revise_schedule


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


def test_warm_start_realistic_mid_cycle_leave():
    """Elena Petrov takes new approved leave covering all of block 3,
    mid-cycle. Revising must respect the new leave and still satisfy every
    hard constraint, while leaving every OTHER resident-block assignment
    exactly as it was — the deviation penalty should mean nothing else
    reshuffles just because Elena's slot opened up."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    with get_session(engine) as session:
        seed(session)
        residents = session.query(Resident).all()
        rotations = session.query(Rotation).all()
        blocks = session.query(Block).all()
        time_off = session.query(TimeOff).all()

    roster = Roster(residents=residents, rotations=rotations, blocks=blocks, time_off=time_off)
    original_schedule = build_full_schedule(roster, year=2026)

    elena_id = next(r.id for r in residents if r.name == "Elena Petrov")
    block_3_id = next(b.id for b in blocks if b.block_number == 3)
    block_3 = next(b for b in blocks if b.id == block_3_id)

    new_time_off = time_off + [
        FakeTimeOff(resident_id=elena_id, start_date=block_3.start_date, end_date=block_3.end_date, type="leave", approved=True)
    ]
    revised_roster = Roster(residents=residents, rotations=rotations, blocks=blocks, time_off=new_time_off)

    current = CurrentFullSchedule(
        roster=revised_roster, year=2026, previous_assignments=original_schedule.assignments
    )
    revised_schedule = revise_schedule(current, perturbations=["Elena Petrov: new leave, block 3"])

    violations = check_all(
        assignments=revised_schedule.assignments, residents=residents, rotations=rotations, blocks=blocks,
        time_off=new_time_off, call_history=[],
    )
    assert violations == []

    assert not any(a.resident_id == elena_id and a.block_id == block_3_id for a in revised_schedule.assignments)

    original_by_key = {(a.resident_id, a.block_id): (a.rotation_id, a.role) for a in original_schedule.assignments}
    revised_by_key = {(a.resident_id, a.block_id): (a.rotation_id, a.role) for a in revised_schedule.assignments}

    changed = {
        key
        for key in set(original_by_key) | set(revised_by_key)
        if original_by_key.get(key) != revised_by_key.get(key)
    }
    assert changed == {(elena_id, block_3_id)}


def test_warm_start_keeps_already_optimal_schedule_unchanged_when_nothing_perturbed():
    """No roster change at all -> revise_schedule should just reproduce the
    previous assignments exactly (deviation cost of anything else is
    strictly worse, and the previous schedule was already fairness-optimal)."""
    residents = [FakeResident(id=1, pgy=1), FakeResident(id=2, pgy=1)]
    rotations = [
        FakeRotation(id=1, name="R1", intern_capacity=1, senior_capacity=0),
        FakeRotation(id=2, name="R2", intern_capacity=2, senior_capacity=0),
    ]
    blocks = _blocks(2026, 4)
    roster = Roster(residents=residents, rotations=rotations, blocks=blocks, time_off=[])

    previous = [
        ProposedAssignment(resident_id=1, block_id=1, rotation_id=1, role="intern"),
        ProposedAssignment(resident_id=1, block_id=2, rotation_id=2, role="intern"),
        ProposedAssignment(resident_id=1, block_id=3, rotation_id=1, role="intern"),
        ProposedAssignment(resident_id=1, block_id=4, rotation_id=2, role="intern"),
        ProposedAssignment(resident_id=2, block_id=1, rotation_id=2, role="intern"),
        ProposedAssignment(resident_id=2, block_id=2, rotation_id=1, role="intern"),
        ProposedAssignment(resident_id=2, block_id=3, rotation_id=2, role="intern"),
        ProposedAssignment(resident_id=2, block_id=4, rotation_id=1, role="intern"),
    ]

    current = CurrentFullSchedule(roster=roster, year=2026, previous_assignments=previous)
    revised = revise_schedule(current)

    previous_keys = {(a.resident_id, a.block_id, a.rotation_id) for a in previous}
    revised_keys = {(a.resident_id, a.block_id, a.rotation_id) for a in revised.assignments}
    assert revised_keys == previous_keys


def test_warm_start_accommodates_new_intern_without_disturbing_others():
    """A new intern joins partway through the year (start_date mid-block-2).
    Revising must give them assignments from when they're active, and must
    not disturb the two existing interns' already-assigned blocks."""
    blocks = _blocks(2026, 3)
    residents = [
        FakeResident(id=1, pgy=1),
        FakeResident(id=2, pgy=1),
    ]
    rotations = [
        FakeRotation(id=1, name="R1", intern_capacity=2, senior_capacity=0),
        FakeRotation(id=2, name="R2", intern_capacity=2, senior_capacity=0),
    ]
    roster = Roster(residents=residents, rotations=rotations, blocks=blocks, time_off=[])
    original = build_full_schedule(roster, year=2026)

    new_intern = FakeResident(id=3, pgy=1, start_date=blocks[1].start_date)
    revised_roster = Roster(residents=residents + [new_intern], rotations=rotations, blocks=blocks, time_off=[])

    current = CurrentFullSchedule(roster=revised_roster, year=2026, previous_assignments=original.assignments)
    revised = revise_schedule(current, perturbations=["new intern starting block 2"])

    violations = check_all(
        assignments=revised.assignments, residents=revised_roster.residents, rotations=rotations, blocks=blocks,
        time_off=[], call_history=[],
    )
    assert violations == []

    # The new intern has no assignment in block 1 (not active yet), but does in 2 and 3.
    assert not any(a.resident_id == 3 and a.block_id == blocks[0].id for a in revised.assignments)
    assert any(a.resident_id == 3 and a.block_id == blocks[1].id for a in revised.assignments)
    assert any(a.resident_id == 3 and a.block_id == blocks[2].id for a in revised.assignments)

    # The two existing residents' assignments are exactly unchanged.
    original_by_key = {(a.resident_id, a.block_id): a.rotation_id for a in original.assignments}
    revised_by_key = {(a.resident_id, a.block_id): a.rotation_id for a in revised.assignments}
    for resident_id in (1, 2):
        for block in blocks:
            key = (resident_id, block.id)
            assert revised_by_key.get(key) == original_by_key.get(key)
