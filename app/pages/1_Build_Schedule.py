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
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from app.auth import get_actor, require_chief_auth
from audit.log import record as audit_record
from db.models import Block, Resident, Rotation, TimeOff, get_engine, get_session
from solver.full_schedule import (
    DEFAULT_FAIRNESS_WEIGHT,
    DEFAULT_PREFERENCE_WEIGHT,
    InfeasibleScheduleError,
    Roster,
    build_full_schedule,
)

require_chief_auth()

st.title("Build Schedule")
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
