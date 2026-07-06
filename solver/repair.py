"""CP-SAT model for call-out / swap repair against an existing valid schedule.

Development Priority #3 (CLAUDE.md) — the highest-ROI, smallest-scope solver;
prove the architecture here before full_schedule.py.

Scope of this first pass: a single open shift (one resident called out for
one day's duty on their rotation). The replacement pool is peers already
assigned to the *same* rotation and block as the sick resident — pulling
someone in from an unrelated rotation ("assist list" pools) is a real Duke
concept that's out of scope for this toy prototype (see CLAUDE.md's seed
data notes). Because peers are already validly assigned to that rotation,
they already satisfy its PGY eligibility — this repair only needs to check
that picking up the extra shift doesn't blow their duty hours, double-book
their day, or clash with approved time off, via solver/rules.py.

All hard-rule feasibility checks route through solver/rules.py — this file
must not reinline duty-hour, double-coverage, or vacation logic (see
CLAUDE.md's "Conventions & Guardrails").
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ortools.sat.python import cp_model

from solver.rules import check_all, rolling_window_hours

DEFAULT_MAX_CANDIDATES = 3


@dataclass
class CurrentSchedule:
    assignments: list
    residents: list
    rotations: list
    blocks: list
    time_off: list
    call_history: list


@dataclass
class OpenShift:
    block_id: int
    rotation_id: int
    role: str
    date: dt.date
    shift_type: str
    hours: float


@dataclass
class SwapProposal:
    resident_id: int
    block_id: int
    rotation_id: int
    role: str
    date: dt.date
    shift_type: str
    hours: float
    projected_window_hours: float
    rank: int
    reason: str


@dataclass
class _HypotheticalShift:
    """Duck-typed stand-in for a call_history row — matches the attributes
    solver/rules.py reads, without depending on db.models."""

    resident_id: int
    date: dt.date
    shift_type: str
    hours: float


def _candidate_pool(current_schedule: CurrentSchedule, open_shift: OpenShift, sick_resident: int) -> list[int]:
    return [
        a.resident_id
        for a in current_schedule.assignments
        if a.block_id == open_shift.block_id
        and a.rotation_id == open_shift.rotation_id
        and a.role == open_shift.role
        and a.resident_id != sick_resident
    ]


def _is_feasible(current_schedule: CurrentSchedule, candidate_call_history: list) -> bool:
    """A candidate's hypothetical call_history is feasible iff it introduces
    no violations beyond whatever the current (already-valid) schedule has."""
    common_kwargs = dict(
        assignments=current_schedule.assignments,
        residents=current_schedule.residents,
        rotations=current_schedule.rotations,
        blocks=current_schedule.blocks,
        time_off=current_schedule.time_off,
    )
    baseline_violations = check_all(call_history=current_schedule.call_history, **common_kwargs)
    candidate_violations = check_all(call_history=candidate_call_history, **common_kwargs)
    new_violations = [v for v in candidate_violations if v not in baseline_violations]
    return not new_violations


def repair_schedule(
    current_schedule: CurrentSchedule,
    open_shift: OpenShift,
    sick_resident: int,
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> list[SwapProposal]:
    """Find replacement(s) for `sick_resident`'s open shift, ranked by
    fewest projected hours in their rolling duty-hour window (i.e. pick the
    least-burdened feasible peer first) — CLAUDE.md's repair objective:
    "minimize disruption... fairness in absorbing the gap." Returns up to
    `max_candidates` ranked proposals, or an empty list if no peer is
    feasible.
    """
    candidate_ids = _candidate_pool(current_schedule, open_shift, sick_resident)

    feasible: dict[int, float] = {}
    for resident_id in candidate_ids:
        hypothetical = current_schedule.call_history + [
            _HypotheticalShift(
                resident_id=resident_id,
                date=open_shift.date,
                shift_type=open_shift.shift_type,
                hours=open_shift.hours,
            )
        ]
        if _is_feasible(current_schedule, hypothetical):
            feasible[resident_id] = rolling_window_hours(resident_id, hypothetical, open_shift.date)

    if not feasible:
        return []

    model = cp_model.CpModel()
    # CP-SAT requires integer coefficients; preserve one decimal of precision.
    scaled_hours = {rid: int(round(hours * 10)) for rid, hours in feasible.items()}
    live_vars = {rid: model.NewBoolVar(f"pick_{rid}") for rid in feasible}
    model.Add(sum(live_vars.values()) == 1)
    model.Minimize(sum(scaled_hours[rid] * var for rid, var in live_vars.items()))

    proposals: list[SwapProposal] = []
    solver = cp_model.CpSolver()
    remaining = dict(live_vars)
    for rank in range(1, min(max_candidates, len(feasible)) + 1):
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break

        chosen = next(rid for rid, var in remaining.items() if solver.Value(var) == 1)
        proposals.append(
            SwapProposal(
                resident_id=chosen,
                block_id=open_shift.block_id,
                rotation_id=open_shift.rotation_id,
                role=open_shift.role,
                date=open_shift.date,
                shift_type=open_shift.shift_type,
                hours=open_shift.hours,
                projected_window_hours=feasible[chosen],
                rank=rank,
                reason=(
                    f"resident {chosen} has the lowest projected rolling-window load "
                    f"({feasible[chosen]}h) among peers on this rotation eligible to cover"
                ),
            )
        )
        model.Add(live_vars[chosen] == 0)
        del remaining[chosen]

    return proposals
