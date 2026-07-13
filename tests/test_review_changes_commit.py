"""Tests for the commit logic in app/pages/3_Review_Changes.py — Development
Priority #10 (CLAUDE.md). This is the only DB-writing code in the whole app
(Pages 1 and 2 only propose and log to audit_log), so it gets its own
regression tests despite being page code, unlike Page 1/2's UI wiring which
just calls already-tested solver functions.

Streamlit page scripts execute top-level on import and expect a real
ScriptRunContext, so these tests don't import the page — they replicate its
commit logic exactly (upsert-in-place for full schedules, insert-only for
swaps) against a real temp-file DB, which is what actually matters: that
the DB ends up in the right state, and that a foreign-key conflict rolls
back cleanly instead of partially applying.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy.exc

from db.models import (
    Assignment,
    Block,
    CallHistory,
    Resident,
    Rotation,
    Swap,
    TimeOff,
    get_engine,
    get_session,
    init_db,
)
from db.seed import seed
from solver.full_schedule import Roster, build_full_schedule
from solver.repair import CurrentSchedule, OpenShift, repair_schedule


@pytest.fixture
def seeded_engine(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/test.db")
    init_db(engine)
    with get_session(engine) as session:
        seed(session)
    return engine


def _commit_swap(engine, pending_swap: dict) -> None:
    with get_session(engine) as session:
        original_assignment = (
            session.query(Assignment)
            .filter_by(resident_id=pending_swap["sick_resident_id"], block_id=pending_swap["open_shift"]["block_id"])
            .one()
        )
        session.add(
            CallHistory(
                resident_id=pending_swap["chosen_resident_id"],
                date=dt.date.fromisoformat(pending_swap["open_shift"]["date"]),
                shift_type=pending_swap["open_shift"]["shift_type"],
                hours=pending_swap["open_shift"]["hours"],
            )
        )
        session.add(
            Swap(
                original_assignment_id=original_assignment.id,
                new_assignment_id=None,
                reason=pending_swap["reason"],
                approved_by="dr_test",
            )
        )
        session.commit()


def _commit_full_schedule(engine, year: int, proposed: dict[tuple[int, int], tuple[int, str]]) -> None:
    with get_session(engine) as session:
        blocks = {b.id for b in session.query(Block).all() if b.year == year}
        existing_by_key = {
            (a.resident_id, a.block_id): a for a in session.query(Assignment).filter(Assignment.block_id.in_(blocks)).all()
        }
        for key, existing in existing_by_key.items():
            if key not in proposed:
                session.delete(existing)
        for (resident_id, block_id), (rotation_id, role) in proposed.items():
            existing = existing_by_key.get((resident_id, block_id))
            if existing is not None:
                existing.rotation_id = rotation_id
                existing.role = role
            else:
                session.add(Assignment(resident_id=resident_id, block_id=block_id, rotation_id=rotation_id, role=role))
        session.commit()


def test_committing_a_swap_writes_call_history_and_swap_row(seeded_engine):
    with get_session(seeded_engine) as session:
        schedule = CurrentSchedule(
            assignments=session.query(Assignment).all(),
            residents=session.query(Resident).all(),
            rotations=session.query(Rotation).all(),
            blocks=session.query(Block).all(),
            time_off=session.query(TimeOff).all(),
            call_history=session.query(CallHistory).all(),
        )
        sick = next(r for r in schedule.residents if r.name == "Elena Petrov")
        a = next(x for x in schedule.assignments if x.resident_id == sick.id)
        open_shift = OpenShift(
            block_id=a.block_id, rotation_id=a.rotation_id, role=a.role,
            date=dt.date(2026, 7, 10), shift_type="night_call", hours=14.0,
        )
        proposals = repair_schedule(schedule, open_shift, sick_resident=sick.id)
        chosen_id = proposals[0].resident_id

    pending_swap = {
        "sick_resident_id": sick.id,
        "chosen_resident_id": chosen_id,
        "open_shift": {
            "block_id": open_shift.block_id, "rotation_id": open_shift.rotation_id, "role": open_shift.role,
            "date": open_shift.date.isoformat(), "shift_type": open_shift.shift_type, "hours": open_shift.hours,
        },
        "reason": proposals[0].reason,
    }
    _commit_swap(seeded_engine, pending_swap)

    with get_session(seeded_engine) as session:
        call_history = session.query(CallHistory).filter_by(resident_id=chosen_id, date=dt.date(2026, 7, 10)).all()
        swaps = session.query(Swap).all()
        assert len(call_history) == 1
        assert call_history[0].hours == 14.0
        assert len(swaps) == 1
        assert swaps[0].original_assignment_id == a.id


def test_committing_a_full_schedule_upserts_in_place_and_is_idempotent_to_rediff(seeded_engine):
    with get_session(seeded_engine) as session:
        roster = Roster(
            residents=session.query(Resident).all(),
            rotations=session.query(Rotation).all(),
            blocks=session.query(Block).all(),
            time_off=session.query(TimeOff).all(),
        )
    schedule = build_full_schedule(roster, year=2026)
    proposed = {(a.resident_id, a.block_id): (a.rotation_id, a.role) for a in schedule.assignments}

    _commit_full_schedule(seeded_engine, 2026, proposed)

    with get_session(seeded_engine) as session:
        blocks = {b.id for b in session.query(Block).all() if b.year == 2026}
        current = {
            (a.resident_id, a.block_id): (a.rotation_id, a.role)
            for a in session.query(Assignment).filter(Assignment.block_id.in_(blocks)).all()
        }

    assert current == proposed
    # Re-diffing against the same proposal should show zero changes.
    assert all(current.get(k) == v for k, v in proposed.items())


def test_full_schedule_commit_rolls_back_cleanly_on_fk_conflict(seeded_engine):
    """A previously committed swap references an assignment by ID. If a new
    full-schedule commit would need to delete that exact assignment (e.g.
    the resident goes on approved leave for that block in the rebuild), the
    delete must fail its foreign-key constraint and the whole commit must
    roll back rather than partially apply."""
    with get_session(seeded_engine) as session:
        schedule = CurrentSchedule(
            assignments=session.query(Assignment).all(),
            residents=session.query(Resident).all(),
            rotations=session.query(Rotation).all(),
            blocks=session.query(Block).all(),
            time_off=session.query(TimeOff).all(),
            call_history=session.query(CallHistory).all(),
        )
        sick = next(r for r in schedule.residents if r.name == "Elena Petrov")
        a = next(x for x in schedule.assignments if x.resident_id == sick.id)
        open_shift = OpenShift(
            block_id=a.block_id, rotation_id=a.rotation_id, role=a.role,
            date=dt.date(2026, 7, 10), shift_type="night_call", hours=14.0,
        )
        proposals = repair_schedule(schedule, open_shift, sick_resident=sick.id)
        chosen_id = proposals[0].resident_id
        original_assignment_id = a.id
        block_id = a.block_id

    _commit_swap(
        seeded_engine,
        {
            "sick_resident_id": sick.id,
            "chosen_resident_id": chosen_id,
            "open_shift": {
                "block_id": open_shift.block_id, "rotation_id": open_shift.rotation_id, "role": open_shift.role,
                "date": open_shift.date.isoformat(), "shift_type": open_shift.shift_type, "hours": open_shift.hours,
            },
            "reason": proposals[0].reason,
        },
    )

    # Force the rebuild to drop the exact (resident, block) the swap references.
    with get_session(seeded_engine) as session:
        block = session.get(Block, block_id)
        session.add(
            TimeOff(resident_id=sick.id, start_date=block.start_date, end_date=block.end_date, type="vacation", approved=True)
        )
        session.commit()
        roster = Roster(
            residents=session.query(Resident).all(),
            rotations=session.query(Rotation).all(),
            blocks=session.query(Block).all(),
            time_off=session.query(TimeOff).all(),
        )

    schedule = build_full_schedule(roster, year=2026)
    proposed = {(a.resident_id, a.block_id): (a.rotation_id, a.role) for a in schedule.assignments}
    assert (sick.id, block_id) not in proposed  # confirms the conflict scenario is real

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        _commit_full_schedule(seeded_engine, 2026, proposed)

    # The assignment must still be there — no partial commit.
    with get_session(seeded_engine) as session:
        still_there = session.get(Assignment, original_assignment_id)
        assert still_there is not None
