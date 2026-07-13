"""Mid-cycle revisions — reuses full_schedule.py's model with a penalty
term on deviation from the current schedule.

Development Priority #11 (CLAUDE.md). Use case: a resident takes leave, a
new intern joins, a rotation capacity changes mid-year — something in the
roster changed after a schedule was already built and committed, and the
chief needs a new schedule that accommodates it without needlessly
reshuffling residents whose situation didn't change.

The caller is responsible for reflecting whatever changed in the `roster`
passed via CurrentFullSchedule (e.g. appending a TimeOff row, adding a new
Resident, editing a Rotation's capacity) before calling revise_schedule —
this module doesn't have a notion of "perturbation types" to apply itself,
it only knows how to solve a new schedule against whatever roster state
it's given while preferring to keep prior assignments. `perturbations` is
caller-facing metadata (e.g. for an audit_log reason) describing *why* a
revision was triggered; it is not consumed by the solver.
"""

from __future__ import annotations

from solver.full_schedule import (
    DEFAULT_FAIRNESS_WEIGHT,
    DEFAULT_MAX_TIME_IN_SECONDS,
    DEFAULT_PREFERENCE_WEIGHT,
    ProposedAssignment,
    Roster,
    Schedule,
    _build_model,
    _solve_and_extract,
)

DEFAULT_DEVIATION_WEIGHT = 5
"""Deviating from an existing, already-lived-with assignment should cost
more than an average unit of fairness-spread churn, so the solver only
changes what the perturbation actually forces rather than opportunistically
reshuffling residents whose circumstances didn't change. Tunable; there's
no principled "correct" value, just a prior that deviation should dominate
ordinary objective noise."""


class CurrentFullSchedule:
    """Bundles what revise_schedule needs to know about the schedule being
    revised: `roster` reflecting the *new* (post-perturbation) state, and
    `previous_assignments` — what was committed before, to minimize
    deviation from."""

    def __init__(self, roster: Roster, year: int, previous_assignments: list[ProposedAssignment]):
        self.roster = roster
        self.year = year
        self.previous_assignments = previous_assignments


def revise_schedule(
    current_schedule: CurrentFullSchedule,
    perturbations: list[str] | None = None,
    preferences: dict[int, dict[int, float]] | None = None,
    *,
    deviation_weight: float = DEFAULT_DEVIATION_WEIGHT,
    fairness_weight: float = DEFAULT_FAIRNESS_WEIGHT,
    preference_weight: float = DEFAULT_PREFERENCE_WEIGHT,
    max_time_in_seconds: float = DEFAULT_MAX_TIME_IN_SECONDS,
) -> Schedule:
    del perturbations  # caller-facing metadata only; see module docstring

    roster = current_schedule.roster
    year = current_schedule.year
    built = _build_model(roster, year, preferences)

    # Deviation term: for each previously assigned (resident, block,
    # rotation) that's still a valid choice under the new roster, penalize
    # NOT keeping it. If the old rotation no longer has a variable at all
    # (resident now ineligible, block now leave-blocked, etc.) there's
    # nothing to penalize — that change is a direct, unavoidable consequence
    # of the perturbation itself, not something the solver chose to do.
    deviation_terms = []
    for prev in current_schedule.previous_assignments:
        var = built.assignment_vars.get((prev.resident_id, prev.block_id, prev.rotation_id))
        if var is not None:
            deviation_terms.append(1 - var)

    built.model.Minimize(
        deviation_weight * sum(deviation_terms)
        + fairness_weight * sum(built.fairness_terms)
        - preference_weight * sum(built.preference_terms)
    )

    return _solve_and_extract(built, roster, year, max_time_in_seconds=max_time_in_seconds)
