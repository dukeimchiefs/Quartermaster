"""Page 1 — Build Schedule.

Development Priority #9 (CLAUDE.md): UI over solver/full_schedule.py
(Priority #8). Reads the live DB, lets the chief tune fairness/preference
weights and optionally per-resident rotation preferences, then displays the
proposed year schedule as a pivot table. Like Page 2, this only proposes —
it writes to audit_log but does not commit anything to assignments; that's
Page 3 / Priority #10.

llm/prompts/schedule_builder.md (translating free-text intent into
preferences, explaining infeasibility in prose) is not wired in yet — this
page is the structured-form step only, matching how Call Out shipped its
structured form (Priority #4) well before free-text parsing (Priority #6).

The built schedule is stored in st.session_state, not just rendered inside
the "Build schedule" button's if-block: Streamlit reruns the whole script
on every widget interaction, so a "stage for review" button nested inside
that block would make the outer condition go false on the very rerun meant
to handle its click. Only one built schedule is staged at a time; building
again replaces it.

Nests Check Day Off Alignment (formerly its own page) as a sub-section
above the schedule builder — unchanged logic, just relocated so it lives
alongside the rest of the annual-schedule workflow rather than as a
separate sidebar entry. The rest of this page's own solver/full_schedule.py
work is on hold; this relocation doesn't touch it.
"""

from __future__ import annotations

import json
import os

import pandas as pd
import streamlit as st

from app.auth import get_actor, require_chief_auth
from audit.log import record as audit_record
from db.models import Block, Resident, Rotation, TimeOff, get_engine, get_session
from real_schedule.checks import check_day_off_alignment
from real_schedule.inpatient_schedule import load_inpatient_week_rows
from real_schedule.roster import RosterIndex, load_roster
from solver.full_schedule import (
    DEFAULT_FAIRNESS_WEIGHT,
    DEFAULT_PREFERENCE_WEIGHT,
    InfeasibleScheduleError,
    Roster,
    build_full_schedule,
)

require_chief_auth()

st.title("Build Schedule")

_RESIDENT_SCHEDULES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "Resident_Schedules")
_INPATIENT_DIR = os.path.join(_RESIDENT_SCHEDULES_DIR, "weekly_INPATIENT_Schedules")
_ROSTER_PATH = os.path.join(_RESIDENT_SCHEDULES_DIR, "duke_residency_2026-2027.csv")

_DAY_OFF_SERVICE_FILES = {
    "VA GM": (os.path.join(_INPATIENT_DIR, "VA", "VA GM", "VA GM 2026-2027.xlsx"), "Sheet1"),
    "DRH GM": (os.path.join(_INPATIENT_DIR, "DRH", "DRH GM 2026-27 Postable.xlsx"), "Sheet1"),
    "VA MICU": (os.path.join(_INPATIENT_DIR, "VA", "VA MICU", "VA MICU 2026-2027.xlsx"), "VA MICU Schedule"),
    "DUH MICU": (os.path.join(_INPATIENT_DIR, "DUH", "MICU", "DUH MICU 2026-27.xlsx"), "Sheet1"),
    "DUH CICU": (os.path.join(_INPATIENT_DIR, "DUH", "CICU", "DUKE CICU 2026-27.xlsx"), "Sheet1"),
}


@st.cache_data(show_spinner="Reading real schedule workbooks...")
def _load_day_off_alignment_data(service_path: str, sheet_name: str, service_mtime: float, roster_mtime: float):
    del service_mtime, roster_mtime  # cache-key only
    roster_entries, w0 = load_roster(_ROSTER_PATH)
    roster = RosterIndex(roster_entries)
    inpatient_week_rows, w1 = load_inpatient_week_rows(service_path, sheet_name, roster=roster)
    return inpatient_week_rows, w0 + w1


def _render_day_off_alignment() -> None:
    """Verifies a proposed "SAC" (Strategic Adjustment of Calendar) request
    per the Well-being Policy: a resident asks for a specific day off during
    an inpatient block. Checks whether the resident's currently-assigned
    team already has that date off, and if not, which other teams on the
    same service/week do. Read-only over the real workbooks at
    Resident_Schedules/ — never writes back to Excel or db.models, every
    check logged to audit_log. Starts with a curated set of 5
    confirmed-working service files (not all ~19 under
    weekly_INPATIENT_Schedules/); expand _DAY_OFF_SERVICE_FILES as more are
    confirmed."""
    if not os.path.isdir(_RESIDENT_SCHEDULES_DIR):
        st.error(f"Resident_Schedules/ not found at {_RESIDENT_SCHEDULES_DIR} — can't read the real schedules.")
        return

    service_label = st.selectbox("Inpatient service", options=sorted(_DAY_OFF_SERVICE_FILES), key="day_off_service")
    service_path, sheet_name = _DAY_OFF_SERVICE_FILES[service_label]

    try:
        mtimes = (os.path.getmtime(service_path), os.path.getmtime(_ROSTER_PATH))
    except OSError as exc:
        st.error(f"Couldn't read one of the real schedule workbooks: {exc}")
        return

    inpatient_week_rows, load_warnings = _load_day_off_alignment_data(service_path, sheet_name, *mtimes)

    if load_warnings:
        with st.expander(f"{len(load_warnings)} parsing warning(s) while reading the real workbooks"):
            for w in load_warnings[:50]:
                st.caption(f"{w.sheet} (row {w.row}): {w.reason}")

    resident_names = sorted({r.resident_name for r in inpatient_week_rows})
    if not resident_names:
        st.warning("No residents could be parsed from this service's schedule.")
        return

    resident_name = st.selectbox("Resident", options=resident_names, key="day_off_resident")

    candidate_dates = sorted({d for r in inpatient_week_rows if r.resident_name == resident_name for d in r.day_parts})
    if not candidate_dates:
        st.warning(f"No dates found for {resident_name} on this service.")
        return

    target_date = st.selectbox(
        "Requested day off", options=candidate_dates, format_func=lambda d: d.strftime("%a, %b %-d, %Y"), key="day_off_date"
    )

    if st.button("Check this day-off request", type="primary", key="check_day_off_alignment"):
        result = check_day_off_alignment(resident_name, target_date, inpatient_week_rows=inpatient_week_rows)

        audit_record(
            actor=get_actor(),
            action="check_day_off_alignment",
            reason=f"checked SAC request: {resident_name} on {service_label}, requested {target_date} off",
            details=json.dumps(
                {
                    "resident_name": resident_name,
                    "service": service_label,
                    "target_date": target_date.isoformat(),
                    "current_team": result.current_team,
                    "currently_off": result.currently_off,
                    "alternative_teams_with_day_off": result.alternative_teams_with_day_off,
                    "is_clear": result.is_clear,
                    "findings": [{"rule": f.rule, "severity": f.severity, "message": f.message} for f in result.findings],
                }
            ),
        )

        if result.currently_off:
            st.success(f"{resident_name}'s current team ({result.current_team}) already has {target_date} off.")
        elif not result.is_clear:
            st.error("Couldn't evaluate this request — review before proceeding.")
        else:
            st.info(f"{resident_name}'s current team ({result.current_team}) does not have {target_date} off.")

        for finding in result.findings:
            container = st.error if finding.severity == "blocking" else st.warning
            container(finding.message)

        st.divider()
        st.caption("Reminders (not machine-checkable):")
        for reminder in result.reminders:
            st.info(reminder)


with st.expander("Check Day Off Alignment (SAC requests)"):
    _render_day_off_alignment()

st.divider()
st.header("Build a full block schedule")
st.caption("Build a full block schedule for a year from scratch.")


@st.cache_resource
def _engine():
    return get_engine()


def _load_roster() -> Roster:
    with get_session(_engine()) as session:
        return Roster(
            residents=session.query(Resident).all(),
            rotations=session.query(Rotation).all(),
            blocks=session.query(Block).all(),
            time_off=session.query(TimeOff).all(),
        )


roster = _load_roster()

if not roster.residents or not roster.blocks:
    st.warning("No schedule data found. Run `python -m db.seed` to load the toy dataset.")
    st.stop()

available_years = sorted({b.year for b in roster.blocks})
year = st.selectbox("Year", options=available_years)

with st.expander("Preferences and weights (optional)"):
    st.caption(
        "Preference scores are per resident, per rotation — higher means more "
        "preferred. The solver still enforces every hard constraint (coverage, "
        "PGY eligibility, capacity, vacation) regardless of what's entered here; "
        "preferences only influence which of the otherwise-valid schedules gets "
        "picked."
    )
    residents_sorted = sorted(roster.residents, key=lambda r: r.name)
    rotation_names = [r.name for r in roster.rotations]
    rotation_id_by_name = {r.name: r.id for r in roster.rotations}

    preference_df = pd.DataFrame(
        0.0,
        index=[r.name for r in residents_sorted],
        columns=rotation_names,
    )
    edited_df = st.data_editor(preference_df)

    col1, col2 = st.columns(2)
    with col1:
        fairness_weight = st.number_input(
            "Fairness weight", min_value=0.0, value=float(DEFAULT_FAIRNESS_WEIGHT), step=0.5
        )
    with col2:
        preference_weight = st.number_input(
            "Preference weight", min_value=0.0, value=float(DEFAULT_PREFERENCE_WEIGHT), step=0.5
        )

if st.button("Build schedule", type="primary"):
    resident_id_by_name = {r.name: r.id for r in residents_sorted}
    preferences: dict[int, dict[int, float]] = {}
    for resident_name, row in edited_df.iterrows():
        for rotation_name, score in row.items():
            if score:
                preferences.setdefault(resident_id_by_name[resident_name], {})[
                    rotation_id_by_name[rotation_name]
                ] = float(score)

    try:
        with st.spinner("Solving..."):
            schedule = build_full_schedule(
                roster,
                year=year,
                preferences=preferences,
                fairness_weight=fairness_weight,
                preference_weight=preference_weight,
            )
    except InfeasibleScheduleError as exc:
        st.error(str(exc))
        st.session_state.pop("built_schedule_result", None)
    else:
        audit_record(
            actor=get_actor(),
            action="propose_full_schedule",
            reason=f"build schedule for year {year}",
            details=json.dumps(
                {
                    "year": year,
                    "fairness_weight": fairness_weight,
                    "preference_weight": preference_weight,
                    "preferences": preferences,
                    "assignment_count": len(schedule.assignments),
                }
            ),
        )
        st.session_state["built_schedule_result"] = {
            "year": year,
            "assignments": [
                {
                    "resident_id": a.resident_id,
                    "block_id": a.block_id,
                    "rotation_id": a.rotation_id,
                    "role": a.role,
                }
                for a in schedule.assignments
            ],
        }

built_result = st.session_state.get("built_schedule_result")
if built_result:
    residents_by_id = {r.id: r for r in roster.residents}
    rotations_by_id = {r.id: r for r in roster.rotations}
    blocks_by_id = {b.id: b for b in roster.blocks}
    result_year = built_result["year"]

    st.success(f"Built a schedule with {len(built_result['assignments'])} assignments for {result_year}.")

    pivot_rows = {}
    for a in built_result["assignments"]:
        resident_name = residents_by_id[a["resident_id"]].name
        block_number = blocks_by_id[a["block_id"]].block_number
        rotation_name = rotations_by_id[a["rotation_id"]].name
        pivot_rows.setdefault(resident_name, {})[f"Block {block_number}"] = f"{rotation_name} ({a['role']})"

    block_columns = [
        f"Block {b.block_number}" for b in sorted(roster.blocks, key=lambda b: b.block_number) if b.year == result_year
    ]
    pivot_df = pd.DataFrame.from_dict(pivot_rows, orient="index", columns=block_columns)
    pivot_df = pivot_df.reindex(sorted(pivot_df.index))
    st.dataframe(pivot_df, use_container_width=True)

    st.subheader("Rotation load per resident")
    load_rows = {}
    for a in built_result["assignments"]:
        resident_name = residents_by_id[a["resident_id"]].name
        rotation_name = rotations_by_id[a["rotation_id"]].name
        load_rows.setdefault(resident_name, {})[rotation_name] = (
            load_rows.setdefault(resident_name, {}).get(rotation_name, 0) + 1
        )
    load_df = pd.DataFrame.from_dict(load_rows, orient="index").fillna(0).astype(int)
    load_df = load_df.reindex(sorted(load_df.index))
    st.dataframe(load_df, use_container_width=True)

    if st.button("Stage this schedule for review", type="primary"):
        st.session_state["pending_full_schedule"] = built_result
        st.success("Staged — go to Review Changes to approve and commit.")
