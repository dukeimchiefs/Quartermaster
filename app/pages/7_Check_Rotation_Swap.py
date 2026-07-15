"""Page 7 — Check Rotation Swap.

Verifies a proposed mutual rotation swap: two residents trading whichever
rotation each is on for a block of weeks (e.g. "I'm on VA GM, you're on AMB
Endo, we want to trade for Block 4") — a generalization of Check Assist
Swap (Page 4) to any rotation, not just assist/jeopardy duty. Read-only
over the real workbooks at Resident_Schedules/, same posture as the other
pages — never writes back to Excel or db.models, every check logged to
audit_log.

Confirmed with the chief resident: same PGY tier required, mutual
two-resident trades only (no one-directional "move me" requests without a
matching partner).
"""

from __future__ import annotations

import json
import os

import streamlit as st

from app.auth import get_actor, require_chief_auth
from audit.log import record as audit_record
from real_schedule.assist_list import load_master_assist_list
from real_schedule.checks import check_rotation_swap
from real_schedule.master_schedule import load_master_schedule
from real_schedule.roster import RosterIndex, load_roster

require_chief_auth()

st.title("Check Rotation Swap")
st.caption("Verify a proposed mutual rotation swap between two residents against the real, live schedule (read-only).")

_RESIDENT_SCHEDULES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "Resident_Schedules")
_MASTER_SCHEDULE_PATH = os.path.join(_RESIDENT_SCHEDULES_DIR, "master_MASTER_Schedule_2026-2027.xlsx")
_MASTER_ASSIST_PATH = os.path.join(_RESIDENT_SCHEDULES_DIR, "master_ASSIST_List_2026-2027.xlsx")
_ROSTER_PATH = os.path.join(_RESIDENT_SCHEDULES_DIR, "duke_residency_2026-2027.csv")

if not os.path.isdir(_RESIDENT_SCHEDULES_DIR):
    st.error(f"Resident_Schedules/ not found at {_RESIDENT_SCHEDULES_DIR} — can't read the real schedules.")
    st.stop()


@st.cache_data(show_spinner="Reading real schedule workbooks...")
def _load_all(master_schedule_mtime: float, master_assist_mtime: float, roster_mtime: float):
    del master_schedule_mtime, master_assist_mtime, roster_mtime  # cache-key only
    roster_entries, w0 = load_roster(_ROSTER_PATH)
    roster = RosterIndex(roster_entries)
    master_schedule, w1 = load_master_schedule(_MASTER_SCHEDULE_PATH, roster=roster)
    master_assist, w2 = load_master_assist_list(_MASTER_ASSIST_PATH, roster=roster)
    return master_schedule, master_assist, w0 + w1 + w2


try:
    mtimes = tuple(os.path.getmtime(p) for p in (_MASTER_SCHEDULE_PATH, _MASTER_ASSIST_PATH, _ROSTER_PATH))
except OSError as exc:
    st.error(f"Couldn't read one of the real schedule workbooks: {exc}")
    st.stop()

master_schedule, master_assist, load_warnings = _load_all(*mtimes)

if load_warnings:
    with st.expander(f"{len(load_warnings)} parsing warning(s) while reading the real workbooks"):
        for w in load_warnings[:50]:
            st.caption(f"{w.sheet} (row {w.row}): {w.reason}")

resident_names = sorted({r.resident_name for r in master_schedule})
week_starts = sorted({r.week_start for r in master_schedule})

if not resident_names or not week_starts:
    st.warning("No residents or weeks could be parsed from the Master Schedule.")
    st.stop()

col1, col2 = st.columns(2)
with col1:
    resident_1 = st.selectbox("Resident #1", options=resident_names)
with col2:
    resident_2 = st.selectbox("Resident #2", options=resident_names, index=min(1, len(resident_names) - 1))

col3, col4 = st.columns(2)
with col3:
    week_start_first = st.selectbox(
        "First week of the swap",
        options=week_starts,
        format_func=lambda d: d.strftime("%b %-d, %Y"),
    )
with col4:
    later_weeks = [w for w in week_starts if w >= week_start_first]
    week_start_last = st.selectbox(
        "Last week of the swap",
        options=later_weeks,
        format_func=lambda d: d.strftime("%b %-d, %Y"),
        index=min(3, len(later_weeks) - 1),
    )

weeks_in_range = [w for w in week_starts if week_start_first <= w <= week_start_last]
st.caption(f"{len(weeks_in_range)} week(s) in this swap: {', '.join(w.strftime('%b %-d') for w in weeks_in_range)}")

if st.button("Check this rotation swap", type="primary"):
    result = check_rotation_swap(
        resident_1,
        resident_2,
        weeks_in_range,
        master_schedule=master_schedule,
        master_assist=master_assist,
    )

    audit_record(
        actor=get_actor(),
        action="check_rotation_swap",
        reason=f"checked rotation swap: {resident_1} <-> {resident_2}, weeks {week_start_first} to {week_start_last}",
        details=json.dumps(
            {
                "resident_1": resident_1,
                "resident_2": resident_2,
                "week_start_first": week_start_first.isoformat(),
                "week_start_last": week_start_last.isoformat(),
                "is_clear": result.is_clear,
                "findings": [{"rule": f.rule, "severity": f.severity, "message": f.message} for f in result.findings],
            }
        ),
    )

    if result.is_clear:
        st.success("No blocking issues found.")
    else:
        st.error("This rotation swap has at least one blocking issue — review before proceeding.")

    for finding in result.findings:
        container = st.error if finding.severity == "blocking" else st.warning
        container(finding.message)

    st.divider()
    st.caption("Reminders (not machine-checkable):")
    for reminder in result.reminders:
        st.info(reminder)
