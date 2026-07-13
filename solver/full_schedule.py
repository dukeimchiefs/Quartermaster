"""Ground-up CP-SAT model for building a block schedule from scratch.

Development Priority #8 (CLAUDE.md). Variables: assignment[resident][block]
[rotation] in {0,1} — exactly one rotation per resident per active,
non-leave block (see Roster/scope notes below). Role ('intern'/'senior')
is derived from PGY via rules.role_for_pgy rather than decided by the
solver, since it isn't a free choice in this program.

Hard constraints modeled: full coverage (every active block gets exactly
one rotation), PGY eligibility (rotation.requires_pgy), rotation capacity
per block/role as a [1, capacity] band rather than just a ceiling (a
rotation with any eligible resident must never go completely unstaffed —
"every resident has exactly one rotation" alone doesn't prevent everyone
piling onto the same rotation and leaving another empty; verified against
this codebase's own seed data that the floor is >=1, NOT "near max
capacity" — total intern capacity across rotations there is 9 against only
4 PGY-1 residents, so requiring near-full simultaneously would be
infeasible by construction, not a real scheduling conflict), approved-
vacation respect (rules.blocked_by_approved_leave — same overlap definition
as check_vacation_respect, so a variable is simply never created for a
leave-blocked resident-block rather than built and then forbidden), and
resident employment window (start_date/end_date).

Scope cuts, decided deliberately rather than guessed at:
- No curriculum/minimum-rotation-exposure requirement. The schema has no
  table for "every PGY-1 must do N blocks of ICU per year" and inventing
  one wasn't asked for — v1 only guarantees full, eligible, capacity- and
  vacation-respecting coverage every block.
- ACGME duty hours are NOT modeled here. They're a day/shift-level metric
  (rules.check_duty_hours reads call_history, which doesn't exist yet for
  a not-yet-lived year) — enforced where it's actually meaningful, at the
  call-shift level, by repair.py once shifts are logged against this
  schedule.
- "Board eligibility" has no concrete rule attached. residents.board_eligibility
  exists in the schema but CLAUDE.md doesn't specify what it should
  restrict, and fabricating one (e.g. "reduced load before boards") would
  be encoding an invented policy as if it were real. Left unimplemented
  until someone specifies what it should actually mean.

Objective: minimize (fairness_weight * rotation-load spread across
eligible residents) - (preference_weight * total preference score). Spread
per rotation is (max assigned blocks - min assigned blocks) among residents
eligible for it — a standard CP-SAT load-balancing idiom — rather than
attempting to model "call burden"/"hardship" for which this program has no
data at block-assignment granularity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from solver.rules import blocked_by_approved_leave, check_all, role_for_pgy

DEFAULT_FAIRNESS_WEIGHT = 1
DEFAULT_PREFERENCE_WEIGHT = 1
DEFAULT_MAX_TIME_IN_SECONDS = 60.0
_PREFERENCE_SCALE = 10  # CP-SAT needs integer coefficients; one decimal of precision.


@dataclass
class Roster:
    residents: list
    rotations: list
    blocks: list
    time_off: list


@dataclass
class ProposedAssignment:
    resident_id: int
    block_id: int
    rotation_id: int
    role: str


@dataclass
class Schedule:
    year: int
    assignments: list[ProposedAssignment] = field(default_factory=list)


class InfeasibleScheduleError(RuntimeError):
    """Raised when no schedule satisfies every hard constraint. CP-SAT
    doesn't hand back a human-readable reason for free, and building
    proper infeasibility diagnostics (e.g. via assumption relaxation) is
    future work — for now this at least fails loudly rather than
    returning a bad or partial schedule."""


def _resident_active_during(resident, block) -> bool:
    if resident.start_date > block.end_date:
        return False
    if resident.end_date is not None and resident.end_date < block.start_date:
        return False
    return True


@dataclass
class _BuiltModel:
    """Everything build_full_schedule and warm_start.revise_schedule share:
    the CP-SAT model with every hard constraint already added, plus
    fairness_terms/preference_terms (unweighted — callers scale and combine
    them, along with any extra terms of their own such as a deviation
    penalty, into a single model.Minimize(...) call)."""

    model: cp_model.CpModel
    blocks: list
    assignment_vars: dict[tuple[int, int, int], cp_model.IntVar]
    residents_by_id: dict
    fairness_terms: list
    preference_terms: list


def _build_model(
    roster: Roster,
    year: int,
    preferences: dict[int, dict[int, float]] | None,
) -> _BuiltModel:
    preferences = preferences or {}
    blocks = sorted((b for b in roster.blocks if b.year == year), key=lambda b: b.block_number)
    if not blocks:
        raise ValueError(f"roster has no blocks for year {year}")

    model = cp_model.CpModel()

    # assignment_vars[(resident_id, block_id, rotation_id)] -> BoolVar,
    # created only for eligible, active, non-leave combinations — an
    # ineligible pairing simply has no variable rather than a variable
    # constrained to 0, which keeps the model smaller and the code that
    # reads it (below) simpler.
    assignment_vars: dict[tuple[int, int, int], cp_model.IntVar] = {}
    vars_by_resident_rotation: dict[tuple[int, int], list[cp_model.IntVar]] = {}
    vars_by_block_rotation_role: dict[tuple[int, int, str], list[cp_model.IntVar]] = {}

    leave_blocked_by_resident = {
        resident.id: blocked_by_approved_leave(resident.id, blocks, roster.time_off) for resident in roster.residents
    }

    for resident in roster.residents:
        role = role_for_pgy(resident.pgy)
        for block in blocks:
            if not _resident_active_during(resident, block):
                continue
            if block.id in leave_blocked_by_resident[resident.id]:
                continue

            eligible_rotations = [
                rotation
                for rotation in roster.rotations
                if rotation.requires_pgy is None or resident.pgy >= rotation.requires_pgy
            ]
            block_vars = []
            for rotation in eligible_rotations:
                var = model.NewBoolVar(f"assign_r{resident.id}_b{block.id}_rot{rotation.id}")
                assignment_vars[(resident.id, block.id, rotation.id)] = var
                block_vars.append(var)
                vars_by_resident_rotation.setdefault((resident.id, rotation.id), []).append(var)
                vars_by_block_rotation_role.setdefault((block.id, rotation.id, role), []).append(var)

            if block_vars:
                model.Add(sum(block_vars) == 1)

    for rotation in roster.rotations:
        for block in blocks:
            for role, capacity in (("intern", rotation.intern_capacity), ("senior", rotation.senior_capacity)):
                role_vars = vars_by_block_rotation_role.get((block.id, rotation.id, role), [])
                if not role_vars:
                    continue
                model.Add(sum(role_vars) <= capacity)
                # Minimum staffing: a rotation with any eligible resident and
                # nonzero capacity must never go completely unstaffed for
                # that role — otherwise "every hard constraint satisfied"
                # schedules can legally leave a whole service empty for a
                # block, which full coverage of *residents* alone doesn't
                # prevent (residents just need one rotation each; nothing
                # otherwise requires any given rotation to get used at all).
                # This is deliberately >=1, not "near capacity": capacities
                # are independent per-rotation ceilings sized for planning
                # flexibility, not a simultaneous fill target — the seed
                # data's own numbers (e.g. 9 total intern capacity across
                # rotations against only 4 PGY-1s) make "near capacity
                # everywhere at once" infeasible by construction.
                if capacity > 0:
                    model.Add(sum(role_vars) >= 1)

    # Preference term: score each created variable by preferences[resident_id][rotation_id].
    preference_terms = []
    for (resident_id, _block_id, rotation_id), var in assignment_vars.items():
        score = preferences.get(resident_id, {}).get(rotation_id)
        if score:
            preference_terms.append(int(round(score * _PREFERENCE_SCALE)) * var)

    # Fairness term: for each rotation, spread = max - min assigned-block
    # count among residents eligible for it.
    fairness_terms = []
    residents_by_id = {r.id: r for r in roster.residents}
    for rotation in roster.rotations:
        eligible_resident_ids = [
            resident.id
            for resident in roster.residents
            if (resident.id, rotation.id) in vars_by_resident_rotation
        ]
        if len(eligible_resident_ids) < 2:
            continue
        counts = []
        for resident_id in eligible_resident_ids:
            count_var = model.NewIntVar(0, len(blocks), f"count_r{resident_id}_rot{rotation.id}")
            model.Add(count_var == sum(vars_by_resident_rotation[(resident_id, rotation.id)]))
            counts.append(count_var)
        max_count = model.NewIntVar(0, len(blocks), f"max_rot{rotation.id}")
        min_count = model.NewIntVar(0, len(blocks), f"min_rot{rotation.id}")
        model.AddMaxEquality(max_count, counts)
        model.AddMinEquality(min_count, counts)
        fairness_terms.append(max_count - min_count)

    return _BuiltModel(
        model=model,
        blocks=blocks,
        assignment_vars=assignment_vars,
        residents_by_id=residents_by_id,
        fairness_terms=fairness_terms,
        preference_terms=preference_terms,
    )


def _solve_and_extract(
    built: _BuiltModel,
    roster: Roster,
    year: int,
    *,
    max_time_in_seconds: float,
) -> Schedule:
    """Shared tail end of build_full_schedule and revise_schedule: solve
    whatever objective the caller already set with model.Minimize(...),
    extract the chosen assignments, and round-trip them through rules.py
    before trusting them."""
    solver = cp_model.CpSolver()
    # See memory: CP-SAT's default multi-threaded search hangs in this
    # sandbox. Always single-threaded, same as repair.py.
    solver.parameters.num_search_workers = 1
    solver.parameters.max_time_in_seconds = max_time_in_seconds
    status = solver.Solve(built.model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise InfeasibleScheduleError(
            f"No feasible schedule found for year {year} ({len(roster.residents)} residents, "
            f"{len(built.blocks)} blocks, {len(roster.rotations)} rotations). Check rotation "
            "capacity against roster size, PGY eligibility coverage, and approved time off overlap."
        )

    assignments = [
        ProposedAssignment(
            resident_id=resident_id,
            block_id=block_id,
            rotation_id=rotation_id,
            role=role_for_pgy(built.residents_by_id[resident_id].pgy),
        )
        for (resident_id, block_id, rotation_id), var in built.assignment_vars.items()
        if solver.Value(var) == 1
    ]

    # Solver output must round-trip through rules.py before it's trusted
    # (CLAUDE.md's "solver output... must round-trip through the solver
    # for validation" guardrail) — this should never fire if the
    # constraints above are translated correctly; it's a safety net against
    # a translation bug, not expected to catch real scheduling problems.
    violations = check_all(
        assignments=assignments,
        residents=roster.residents,
        rotations=roster.rotations,
        blocks=built.blocks,
        time_off=roster.time_off,
        call_history=[],
    )
    if violations:
        raise AssertionError(
            f"solver produced a schedule rules.py rejects — this is a solver bug, "
            f"not a data problem: {violations}"
        )

    return Schedule(year=year, assignments=assignments)


def build_full_schedule(
    roster: Roster,
    year: int,
    preferences: dict[int, dict[int, float]] | None = None,
    *,
    fairness_weight: float = DEFAULT_FAIRNESS_WEIGHT,
    preference_weight: float = DEFAULT_PREFERENCE_WEIGHT,
    max_time_in_seconds: float = DEFAULT_MAX_TIME_IN_SECONDS,
) -> Schedule:
    built = _build_model(roster, year, preferences)
    built.model.Minimize(
        fairness_weight * sum(built.fairness_terms) - preference_weight * sum(built.preference_terms)
        if built.fairness_terms or built.preference_terms
        else 0
    )
    return _solve_and_extract(built, roster, year, max_time_in_seconds=max_time_in_seconds)
