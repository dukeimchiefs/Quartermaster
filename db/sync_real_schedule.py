"""Sync the toy DB (db/models.py) from the real, live Master Schedule under
Resident_Schedules/, so Call Out's CP-SAT repair solver (solver/repair.py)
finds coverage candidates against the real roster instead of db/seed.py's
fictional fixtures.

Run manually: `python -m db.sync_real_schedule`. Not triggered automatically
on page load — a DB write should be a deliberate, auditable step, not a side
effect of opening Call Out.

Scope and known gaps — this exists so Call Out's repair solver has real
assignments to find peers against, not to model everything the real
schedule contains:

- Residents/rotations/blocks/assignments come from the Master Schedule +
  roster CSV. The rotation catalog is whatever real, non-committing-label
  values (see real_schedule.common.is_non_committing_label) actually appear
  in the data, not filtered against the Master Schedule's own `Validation
  Lists` taxonomy — that sheet validates the workbook's own dropdowns, but
  every value that made it into a resident's row is already real. Only
  upserts (adds new rows, updates
  changed ones) — never deletes an existing assignment/rotation/resident,
  even if a later real-schedule snapshot no longer shows it. A resident
  whose real assignment for a block is now "VAC"/"LOA"/jeopardy (see
  real_schedule.common.is_non_committing_label) simply isn't touched that
  block, rather than removed, to avoid the foreign-key cascade risk of
  deleting an assignment a committed Swap row still references. A full
  refresh means starting from a fresh (or wiped) database, same as
  db.seed's own `init_db` convention.
- No real rotation-capacity data exists anywhere in Resident_Schedules/, so
  intern_capacity/senior_capacity are set to the *observed* max concurrent
  headcount per rotation/role across all blocks in this sync — non-blocking
  by construction (every occupant already there fits by definition), but
  NOT a verified real staffing cap. This only needs to avoid falsely
  blocking existing occupancy, since Call Out's repair.py only pulls
  candidates from residents already validly on the same rotation/block
  (solver/repair.py:_candidate_pool), not a from-scratch capacity solve.
- Call history is intentionally left empty: no per-shift, per-hour duty log
  exists in the current Excel workbooks (Pulls_this_year and the call-out
  log in real_schedule/assist_list.py aren't shift-level hours). Until a
  real shift-level log exists, Call Out's "least-burdened" ranking is a
  flat tie-break among same-rotation peers — the blocking rules (PGY,
  rotation capacity as described above, vacation) are still correct, only
  the ranking signal is degraded.
- db/models.py's Block is nominally a 4-week academic block (see
  db/seed.py's toy fixtures), but real rotations here change on a ~2-week
  (sometimes 1-week) cadence, confirmed live against the actual Master
  Schedule (e.g. one resident: "VA GM" x2 weeks, "AMB Endo" x2, "Duke CICU"
  x2, "Duke GM" x2, all inside what would be one naive 4-week grouping).
  Grouping into synthetic 4-week spans would silently misassign several of
  those weeks to the wrong rotation, which directly breaks Call Out's
  candidate pool (solver/repair.py:_candidate_pool matches by exact
  rotation_id + block_id — the wrong rotation means the wrong pool of
  peers). So each DB `Block` row here is exactly one real Master Schedule
  week, not a 4-week span: correctness for Call Out's per-date candidate
  pool matters more than matching Build Schedule's "Block 1..6" annual
  framing, which is that module's own concern and is on hold regardless.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections import defaultdict

from sqlalchemy.orm import Session

from db.models import (
    Assignment,
    AuditLog,
    Block,
    Resident,
    Rotation,
    TimeOff,
    get_engine,
    get_session,
    init_db,
)
from real_schedule.common import is_non_committing_label
from real_schedule.master_schedule import load_master_schedule
from real_schedule.roster import RosterIndex, load_roster
from solver.rules import role_for_pgy

_RESIDENT_SCHEDULES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Resident_Schedules")
_MASTER_SCHEDULE_PATH = os.path.join(_RESIDENT_SCHEDULES_DIR, "master_MASTER_Schedule_2026-2027.xlsx")
_ROSTER_PATH = os.path.join(_RESIDENT_SCHEDULES_DIR, "duke_residency_2026-2027.csv")

_VALID_PGY = (1, 2, 3, 4)


def _academic_year(week_start: dt.date) -> int:
    return week_start.year if week_start.month >= 7 else week_start.year - 1


def _leave_type(label: str) -> str:
    text = label.strip().upper()
    if text.startswith("VAC"):
        return "vacation"
    if text.startswith("LOA"):
        return "loa"
    return "other"


def _leave_runs(entries: list[tuple[dt.date, str]]) -> list[tuple[dt.date, dt.date, str]]:
    """Collapse a resident's sorted (week_start, rotation_label) VAC/LOA
    weeks into contiguous (start, end, type) spans — consecutive weeks (7
    days apart) of the same leave type merge into one TimeOff row rather
    than one row per week."""
    entries = sorted(entries)
    runs: list[tuple[dt.date, dt.date, str]] = []
    i = 0
    while i < len(entries):
        start_week, label = entries[i]
        leave_type = _leave_type(label)
        end_week = start_week
        j = i + 1
        while (
            j < len(entries)
            and entries[j][0] == end_week + dt.timedelta(days=7)
            and _leave_type(entries[j][1]) == leave_type
        ):
            end_week = entries[j][0]
            j += 1
        runs.append((start_week, end_week + dt.timedelta(days=6), leave_type))
        i = j
    return runs


def sync(
    session: Session,
    *,
    master_schedule_path: str = _MASTER_SCHEDULE_PATH,
    roster_path: str = _ROSTER_PATH,
    academic_year_start: int = 2026,
) -> dict[str, int]:
    """`academic_year_start` is the calendar year the academic year begins in
    (e.g. 2026 for AY2026-2027, matching db/seed.py's own convention).
    master_MASTER_Schedule's week columns span a rolling ~12-month window
    that straddles two academic years (confirmed live: this file's Jan-Jun
    2026 weeks belong to the *prior* AY2025-26, not AY2026-27) — records
    outside the target academic year are dropped up front, before PGY
    resolution or block grouping, so a resident's stale prior-year PGY or
    rotation can't leak into this sync."""
    roster_entries, roster_warnings = load_roster(roster_path)
    roster = RosterIndex(roster_entries)
    all_records, schedule_warnings = load_master_schedule(master_schedule_path, roster=roster)
    records = [r for r in all_records if _academic_year(r.week_start) == academic_year_start]
    if not records:
        raise RuntimeError(
            f"No Master Schedule weeks found for academic year {academic_year_start}-{academic_year_start + 1}."
        )
    warning_count = len(roster_warnings) + len(schedule_warnings)

    pgy_by_resident: dict[str, int] = {}
    skipped_bad_pgy = 0
    for record in records:
        if record.resident_name in pgy_by_resident or record.pgy is None:
            continue
        if record.pgy not in _VALID_PGY:
            skipped_bad_pgy += 1
            continue
        pgy_by_resident[record.resident_name] = record.pgy

    resident_names = sorted(pgy_by_resident)
    if not resident_names:
        raise RuntimeError("No residents with a resolvable PGY (1-4) found in the Master Schedule — nothing to sync.")

    rotation_by_resident_week: dict[tuple[str, dt.date], str] = {
        (r.resident_name, r.week_start): r.rotation for r in records if r.resident_name in pgy_by_resident
    }

    week_starts = sorted({r.week_start for r in records})
    block_number_by_week = {week: i + 1 for i, week in enumerate(week_starts)}

    # One Block per real week (see module docstring) — no aggregation, so no
    # risk of a multi-week grouping picking up the wrong week's rotation.
    block_rotation: dict[tuple[str, int], str] = {
        (name, block_number_by_week[week]): rotation
        for (name, week), rotation in rotation_by_resident_week.items()
    }

    real_rotation_names = sorted(
        {rotation for rotation in block_rotation.values() if not is_non_committing_label(rotation)}
    )

    # Observed max concurrent headcount per (rotation, role) — see module
    # docstring: a non-blocking stand-in for real capacity data, which
    # doesn't exist anywhere in Resident_Schedules/.
    counts: dict[tuple[str, int, str], int] = defaultdict(int)
    for (name, block_number), rotation in block_rotation.items():
        if is_non_committing_label(rotation):
            continue
        counts[(rotation, block_number, role_for_pgy(pgy_by_resident[name]))] += 1
    capacity_intern: dict[str, int] = defaultdict(int)
    capacity_senior: dict[str, int] = defaultdict(int)
    for (rotation, _block_number, role), count in counts.items():
        if role == "intern":
            capacity_intern[rotation] = max(capacity_intern[rotation], count)
        else:
            capacity_senior[rotation] = max(capacity_senior[rotation], count)

    # Leave weeks (VAC/LOA), collected across every week in the file, not
    # just each block's first week — a resident's leave calendar shouldn't
    # be truncated to block boundaries.
    leave_entries: dict[str, list[tuple[dt.date, str]]] = defaultdict(list)
    for (name, week_start), rotation in rotation_by_resident_week.items():
        if _leave_type(rotation) in ("vacation", "loa"):
            leave_entries[name].append((week_start, rotation))

    residents_by_name: dict[str, Resident] = {}
    for name in resident_names:
        resident = session.query(Resident).filter_by(name=name).one_or_none()
        if resident is None:
            resident = Resident(name=name, pgy=pgy_by_resident[name], start_date=dt.date(academic_year_start, 7, 1))
            session.add(resident)
        else:
            resident.pgy = pgy_by_resident[name]
        residents_by_name[name] = resident
    session.flush()

    rotations_by_name: dict[str, Rotation] = {}
    for rotation_name in real_rotation_names:
        rotation = session.query(Rotation).filter_by(name=rotation_name).one_or_none()
        intern_cap, senior_cap = capacity_intern.get(rotation_name, 0), capacity_senior.get(rotation_name, 0)
        if rotation is None:
            rotation = Rotation(name=rotation_name, intern_capacity=intern_cap, senior_capacity=senior_cap)
            session.add(rotation)
        else:
            rotation.intern_capacity = max(rotation.intern_capacity, intern_cap)
            rotation.senior_capacity = max(rotation.senior_capacity, senior_cap)
        rotations_by_name[rotation_name] = rotation
    session.flush()

    blocks_by_number: dict[int, Block] = {}
    for week in week_starts:
        block_number = block_number_by_week[week]
        start_date, end_date = week, week + dt.timedelta(days=6)
        block = (
            session.query(Block).filter_by(year=academic_year_start, block_number=block_number).one_or_none()
        )
        if block is None:
            block = Block(year=academic_year_start, block_number=block_number, start_date=start_date, end_date=end_date)
            session.add(block)
        else:
            block.start_date, block.end_date = start_date, end_date
        blocks_by_number[block_number] = block
    session.flush()

    assignments_added = assignments_updated = 0
    for (name, block_number), rotation_label in block_rotation.items():
        if is_non_committing_label(rotation_label):
            continue
        resident = residents_by_name[name]
        block = blocks_by_number[block_number]
        rotation = rotations_by_name.get(rotation_label)
        if rotation is None:
            continue
        role = role_for_pgy(resident.pgy)
        existing = session.query(Assignment).filter_by(resident_id=resident.id, block_id=block.id).one_or_none()
        if existing is None:
            session.add(Assignment(resident=resident, block=block, rotation=rotation, role=role))
            assignments_added += 1
        elif existing.rotation_id != rotation.id or existing.role != role:
            existing.rotation_id = rotation.id
            existing.role = role
            assignments_updated += 1

    time_off_added = 0
    for name, entries in leave_entries.items():
        resident = residents_by_name.get(name)
        if resident is None:
            continue
        for start_date, end_date, leave_type in _leave_runs(entries):
            exists = (
                session.query(TimeOff)
                .filter_by(resident_id=resident.id, start_date=start_date, end_date=end_date, type=leave_type)
                .one_or_none()
            )
            if exists is None:
                session.add(TimeOff(resident=resident, start_date=start_date, end_date=end_date, type=leave_type, approved=True))
                time_off_added += 1

    summary = {
        "residents": len(resident_names),
        "residents_skipped_bad_pgy": skipped_bad_pgy,
        "rotations": len(real_rotation_names),
        "blocks": len(week_starts),
        "assignments_added": assignments_added,
        "assignments_updated": assignments_updated,
        "time_off_added": time_off_added,
        "parse_warnings": warning_count,
    }
    session.add(
        AuditLog(
            actor="system",
            action="sync_real_schedule",
            reason="synced the live Master Schedule + roster into the DB",
            details=json.dumps(summary),
        )
    )
    session.commit()
    return summary


def main() -> None:
    engine = get_engine()
    init_db(engine)
    with get_session(engine) as session:
        summary = sync(session)
        for key, value in summary.items():
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
