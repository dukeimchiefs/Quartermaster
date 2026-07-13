"""Page 2 — Call Out.

Development Priority #4 (CLAUDE.md): structured form + result display, ahead
of the LLM-driven free-text path (Priority #6). Reads the live DB, calls
solver/repair.py, and displays ranked replacement proposals. This page does
not commit anything — no assignment or call_history row is written, and
correspondingly nothing is written to audit_log yet (that's Priority #7,
which only applies to actual state changes). Approving/committing a swap is
future work (Page 3 / Priority #10).
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

from app.auth import require_chief_auth
from db.models import Assignment, Block, CallHistory, Resident, Rotation, TimeOff, get_engine, get_session
from solver.repair import CurrentSchedule, OpenShift, repair_schedule

require_chief_auth()

st.title("Call Out")
st.caption("Find replacement coverage for a resident who's out.")


@st.cache_resource
def _engine():
    return get_engine()


def _load_schedule() -> CurrentSchedule:
    with get_session(_engine()) as session:
        return CurrentSchedule(
            assignments=session.query(Assignment).all(),
            residents=session.query(Resident).all(),
            rotations=session.query(Rotation).all(),
            blocks=session.query(Block).all(),
            time_off=session.query(TimeOff).all(),
            call_history=session.query(CallHistory).all(),
        )


schedule = _load_schedule()

if not schedule.residents or not schedule.assignments:
    st.warning("No schedule data found. Run `python -m db.seed` to load the toy dataset.")
    st.stop()

rotations_by_id = {r.id: r for r in schedule.rotations}
blocks_by_id = {b.id: b for b in schedule.blocks}

sick_resident = st.selectbox(
    "Who's out?",
    options=schedule.residents,
    format_func=lambda r: f"{r.name} (PGY-{r.pgy})",
)

their_assignments = [a for a in schedule.assignments if a.resident_id == sick_resident.id]
if not their_assignments:
    st.warning(f"{sick_resident.name} has no block assignment in the current schedule.")
    st.stop()

assignment = st.selectbox(
    "Which assignment needs coverage?",
    options=their_assignments,
    format_func=lambda a: (
        f"Block {blocks_by_id[a.block_id].block_number} — "
        f"{rotations_by_id[a.rotation_id].name} ({a.role})"
    ),
)
block = blocks_by_id[assignment.block_id]
rotation = rotations_by_id[assignment.rotation_id]

col1, col2, col3 = st.columns(3)
with col1:
    shift_date = st.date_input(
        "Shift date",
        value=block.start_date,
        min_value=block.start_date,
        max_value=block.end_date,
    )
with col2:
    shift_type = st.text_input("Shift type", value="night_call")
with col3:
    hours = st.number_input("Hours", min_value=1.0, max_value=30.0, value=14.0, step=1.0)

if st.button("Find coverage", type="primary"):
    open_shift = OpenShift(
        block_id=block.id,
        rotation_id=rotation.id,
        role=assignment.role,
        date=shift_date if isinstance(shift_date, dt.date) else shift_date[0],
        shift_type=shift_type,
        hours=hours,
    )
    proposals = repair_schedule(schedule, open_shift, sick_resident=sick_resident.id)

    if not proposals:
        st.error(
            f"No eligible peer found to cover {rotation.name} ({assignment.role}) "
            f"in block {block.block_number}."
        )
    else:
        st.success(f"Found {len(proposals)} candidate(s).")
        residents_by_id = {r.id: r for r in schedule.residents}
        for proposal in proposals:
            candidate = residents_by_id[proposal.resident_id]
            with st.container(border=True):
                st.markdown(f"**#{proposal.rank} — {candidate.name}** (PGY-{candidate.pgy})")
                st.caption(proposal.reason)
                st.metric(
                    f"Projected hours in {proposal.date}'s rolling window",
                    f"{proposal.projected_window_hours:.1f}h",
                )
