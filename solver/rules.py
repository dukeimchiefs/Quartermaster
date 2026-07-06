"""Shared constraint definitions for all three solvers.

Single source of truth for ACGME rules and program-specific policies.
full_schedule.py, repair.py, and warm_start.py must import constraints from
here rather than inlining them — see CLAUDE.md's "Conventions & Guardrails".

These are pure-Python validators: each `check_*` function takes a candidate
schedule (plus whatever context it needs — residents, rotations, blocks,
time off, call history) and returns a list of `Violation`s. They don't
depend on OR-Tools or any particular solver. Full-schedule / repair /
warm-start builds will translate these same checks into CP-SAT constraints
once OR-Tools is pulled in (pending the dependency network/telemetry audit
per CLAUDE.md); until then, they're also how solver output gets validated
before it's allowed to touch the DB.

Inputs are duck-typed against db/models.py's ORM attributes (resident_id,
block_id, rotation_id, role, hours, date, approved, start_date, end_date,
pgy, intern_capacity, senior_capacity, requires_pgy, name, id) rather than
importing the SQLAlchemy classes directly, so callers can pass either real
ORM rows or lightweight stand-ins (e.g. in tests or a repair solver's
in-memory candidate schedule).

Development Priority #2 (CLAUDE.md).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass

DEFAULT_MAX_HOURS_PER_SHIFT = 24
DEFAULT_MAX_HOURS_PER_WEEK = 80
DEFAULT_DUTY_WINDOW_DAYS = 7


@dataclass(frozen=True)
class Violation:
    rule: str
    message: str
    resident_id: int | None = None
    block_id: int | None = None


def rolling_window_hours(
    resident_id: int,
    call_history,
    as_of_date: dt.date,
    *,
    window_days: int = DEFAULT_DUTY_WINDOW_DAYS,
) -> float:
    """Total hours `resident_id` has logged in the `window_days`-day window
    ending on `as_of_date` (inclusive). Shared by `check_duty_hours` and by
    repair.py's candidate ranking (least-burdened-first), so the two stay
    consistent with a single definition of "burden"."""
    window_start = as_of_date - dt.timedelta(days=window_days - 1)
    return sum(
        entry.hours
        for entry in call_history
        if entry.resident_id == resident_id and window_start <= entry.date <= as_of_date
    )


def check_duty_hours(
    call_history,
    *,
    max_hours_per_shift: float = DEFAULT_MAX_HOURS_PER_SHIFT,
    max_hours_per_week: float = DEFAULT_MAX_HOURS_PER_WEEK,
    window_days: int = DEFAULT_DUTY_WINDOW_DAYS,
) -> list[Violation]:
    """ACGME duty-hour limits: no single shift over `max_hours_per_shift`,
    and no rolling `window_days`-day total over `max_hours_per_week`."""
    violations: list[Violation] = []

    by_resident: dict[int, list] = defaultdict(list)
    for entry in call_history:
        by_resident[entry.resident_id].append(entry)
        if entry.hours > max_hours_per_shift:
            violations.append(
                Violation(
                    rule="duty_hours",
                    message=(
                        f"resident {entry.resident_id} logged {entry.hours}h on "
                        f"{entry.date}, exceeds {max_hours_per_shift}h/shift limit"
                    ),
                    resident_id=entry.resident_id,
                )
            )

    for resident_id, entries in by_resident.items():
        worst_total = 0.0
        worst_end: dt.date | None = None
        for entry in entries:
            total = rolling_window_hours(resident_id, entries, entry.date, window_days=window_days)
            if total > worst_total:
                worst_total = total
                worst_end = entry.date
        if worst_total > max_hours_per_week:
            violations.append(
                Violation(
                    rule="duty_hours",
                    message=(
                        f"resident {resident_id} logged {worst_total}h in the "
                        f"{window_days}-day window ending {worst_end}, exceeds "
                        f"{max_hours_per_week}h/week limit"
                    ),
                    resident_id=resident_id,
                )
            )

    return violations


def check_no_same_day_double_shift(call_history) -> list[Violation]:
    """A resident may not be logged for more than one shift on the same
    date. This is the call_history-level analogue of `check_no_double_coverage`
    (which operates on block-level `assignments`) — used by repair.py, where
    a call-out is covered by picking up an extra shift rather than a full
    block reassignment."""
    violations: list[Violation] = []
    seen: dict[tuple[int, dt.date], object] = {}
    for entry in call_history:
        key = (entry.resident_id, entry.date)
        if key in seen:
            violations.append(
                Violation(
                    rule="no_double_coverage",
                    message=(
                        f"resident {entry.resident_id} has more than one shift "
                        f"logged on {entry.date}"
                    ),
                    resident_id=entry.resident_id,
                )
            )
        else:
            seen[key] = entry
    return violations


def check_no_double_coverage(assignments) -> list[Violation]:
    """A resident may hold at most one assignment per block."""
    violations: list[Violation] = []
    seen: dict[tuple[int, int], object] = {}
    for assignment in assignments:
        key = (assignment.resident_id, assignment.block_id)
        if key in seen:
            violations.append(
                Violation(
                    rule="no_double_coverage",
                    message=(
                        f"resident {assignment.resident_id} has more than one "
                        f"assignment in block {assignment.block_id}"
                    ),
                    resident_id=assignment.resident_id,
                    block_id=assignment.block_id,
                )
            )
        else:
            seen[key] = assignment
    return violations


def check_rotation_requirements(assignments, rotations, residents) -> list[Violation]:
    """Rotation capacity (per block, per role) and PGY eligibility
    (`rotation.requires_pgy`) are respected."""
    violations: list[Violation] = []
    rotation_by_id = {r.id: r for r in rotations}
    resident_by_id = {r.id: r for r in residents}
    counts: dict[tuple[int, int, str], int] = defaultdict(int)

    for assignment in assignments:
        rotation = rotation_by_id.get(assignment.rotation_id)
        resident = resident_by_id.get(assignment.resident_id)
        if rotation is None or resident is None:
            continue

        if rotation.requires_pgy is not None and resident.pgy < rotation.requires_pgy:
            violations.append(
                Violation(
                    rule="rotation_requirements",
                    message=(
                        f"resident {resident.id} (PGY-{resident.pgy}) assigned to "
                        f"{rotation.name}, which requires PGY-{rotation.requires_pgy}+"
                    ),
                    resident_id=resident.id,
                    block_id=assignment.block_id,
                )
            )

        counts[(assignment.block_id, assignment.rotation_id, assignment.role)] += 1

    for (block_id, rotation_id, role), count in counts.items():
        rotation = rotation_by_id.get(rotation_id)
        if rotation is None:
            continue
        capacity = rotation.intern_capacity if role == "intern" else rotation.senior_capacity
        if count > capacity:
            violations.append(
                Violation(
                    rule="rotation_requirements",
                    message=(
                        f"{rotation.name} block {block_id} has {count} {role}s "
                        f"assigned, capacity is {capacity}"
                    ),
                    block_id=block_id,
                )
            )

    return violations


def check_vacation_respect(assignments, time_off, blocks) -> list[Violation]:
    """No assignment may overlap a resident's approved time off."""
    violations: list[Violation] = []
    block_by_id = {b.id: b for b in blocks}
    approved_time_off = [t for t in time_off if t.approved]

    for assignment in assignments:
        block = block_by_id.get(assignment.block_id)
        if block is None:
            continue
        for off in approved_time_off:
            if off.resident_id != assignment.resident_id:
                continue
            if off.start_date <= block.end_date and off.end_date >= block.start_date:
                violations.append(
                    Violation(
                        rule="vacation_respect",
                        message=(
                            f"resident {assignment.resident_id} assigned in block "
                            f"{block.block_number} overlaps approved time off "
                            f"{off.start_date}–{off.end_date}"
                        ),
                        resident_id=assignment.resident_id,
                        block_id=assignment.block_id,
                    )
                )

    return violations


def check_all(*, assignments, residents, rotations, blocks, time_off, call_history) -> list[Violation]:
    """Run every hard rule against a candidate schedule. Used to validate
    solver output — and any LLM-proposed change — before it's allowed to
    reach the DB (see CLAUDE.md: "LLM output... must round-trip through the
    solver for validation")."""
    return [
        *check_duty_hours(call_history),
        *check_no_double_coverage(assignments),
        *check_no_same_day_double_shift(call_history),
        *check_rotation_requirements(assignments, rotations, residents),
        *check_vacation_respect(assignments, time_off, blocks),
    ]
