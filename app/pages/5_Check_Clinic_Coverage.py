"""Page 5 — Check Clinic Coverage.

Verifies a proposed reassignment when an ambulatory preceptor calls out:
is a resident actually affected, is the candidate preceptor/clinic really
available that half-day, is there already someone else placed there, and
does the candidate slot's tier plausibly fit. Read-only over the real
workbooks at Resident_Schedules/, same posture as Page 4 — never writes
back to Excel or db.models, every check logged to audit_log.

The "available clinics" file is seasonal (Fall 2026 now, a Spring version
later) — this page globs for it rather than hardcoding the filename.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os

import openpyxl
import streamlit as st

from app.auth import get_actor, require_chief_auth
from audit.log import record as audit_record
from real_schedule.ambulatory import load_ambulatory_week
from real_schedule.available_clinics import load_available_clinics
from real_schedule.checks import check_clinic_reassignment
from real_schedule.common import is_preceptor_cell
from real_schedule.roster import RosterIndex, load_roster

require_chief_auth()

st.title("Check Clinic Coverage")
st.caption("Verify a proposed resident reassignment after an ambulatory preceptor calls out (read-only).")

_RESIDENT_SCHEDULES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "Resident_Schedules")
_AMBULATORY_PATH = os.path.join(_RESIDENT_SCHEDULES_DIR, "master_AMBULATORY_schedule_2026-2027.xlsx")
_ROSTER_PATH = os.path.join(_RESIDENT_SCHEDULES_DIR, "duke_residency_2026-2027.csv")
_NON_WEEK_SHEETS = ("Master", "Preferred Clinics", "Build")

if not os.path.isdir(_RESIDENT_SCHEDULES_DIR):
    st.error(f"Resident_Schedules/ not found at {_RESIDENT_SCHEDULES_DIR} — can't read the real schedules.")
    st.stop()

available_clinics_paths = sorted(glob.glob(os.path.join(_RESIDENT_SCHEDULES_DIR, "Available Clinics *.xlsx")))
if not available_clinics_paths:
    st.error("No 'Available Clinics *.xlsx' file found in Resident_Schedules/.")
    st.stop()
available_clinics_path = st.selectbox(
    "Available Clinics list to check against",
    options=available_clinics_paths,
    format_func=os.path.basename,
)

try:
    wb = openpyxl.load_workbook(_AMBULATORY_PATH, read_only=True)
    week_sheet_names = [name for name in wb.sheetnames if name not in _NON_WEEK_SHEETS]
except OSError as exc:
    st.error(f"Couldn't read the ambulatory schedule workbook: {exc}")
    st.stop()

week_sheet_name = st.selectbox("Week", options=week_sheet_names)


@st.cache_data(show_spinner="Reading real schedule workbooks...")
def _load_week(ambulatory_mtime: float, week_sheet_name: str, clinics_path: str, clinics_mtime: float, roster_mtime: float):
    del ambulatory_mtime, clinics_mtime, roster_mtime  # cache-key only
    roster_entries, w0 = load_roster(_ROSTER_PATH)
    roster = RosterIndex(roster_entries)
    ambulatory_week, w1 = load_ambulatory_week(_AMBULATORY_PATH, week_sheet_name, roster=roster)
    available_clinics, w2 = load_available_clinics(clinics_path)
    return ambulatory_week, available_clinics, w0 + w1 + w2


ambulatory_week, available_clinics, load_warnings = _load_week(
    os.path.getmtime(_AMBULATORY_PATH), week_sheet_name, available_clinics_path, os.path.getmtime(available_clinics_path), os.path.getmtime(_ROSTER_PATH)
)

if load_warnings:
    with st.expander(f"{len(load_warnings)} parsing warning(s) while reading the real workbooks"):
        for w in load_warnings[:50]:
            st.caption(f"{w.sheet} (row {w.row}): {w.reason}")

preceptor_names = sorted(
    {
        is_preceptor_cell(cell)[0]
        for row in ambulatory_week
        for cell in row.day_parts.values()
        if is_preceptor_cell(cell) is not None
    }
)
candidate_names = sorted({slot.preceptor_name for slot in available_clinics})

if not preceptor_names:
    st.warning("No preceptor-attributed clinic sessions found in this week's ambulatory sheet.")
    st.stop()

called_out_preceptor = st.selectbox("Preceptor who called out", options=preceptor_names)

col1, col2 = st.columns(2)
with col1:
    weekday_names = ["Mon", "Tues", "Wed", "Thurs", "Fri"]
    weekday = st.selectbox("Day", options=weekday_names)
with col2:
    half_day = st.selectbox("Half-day", options=["AM", "PM"])

# Resolve the actual date for the chosen weekday from this week's parsed rows.
candidate_dates = sorted({d for row in ambulatory_week for (d, h) in row.day_parts if h == half_day})
weekday_index = weekday_names.index(weekday)
matching_dates = [d for d in candidate_dates if d.weekday() == weekday_index]
if not matching_dates:
    st.warning(f"No {weekday} date found in this week's sheet.")
    st.stop()
date_ = matching_dates[0]

col3, col4 = st.columns(2)
with col3:
    candidate_preceptor = st.selectbox("Candidate preceptor", options=candidate_names)
with col4:
    candidate_locations = sorted(
        {slot.location for slot in available_clinics if slot.preceptor_name == candidate_preceptor and slot.location}
    )
    candidate_location = st.selectbox("Candidate location", options=candidate_locations or ["(none listed)"])

if st.button("Check this reassignment", type="primary"):
    result = check_clinic_reassignment(
        called_out_preceptor,
        date_,
        half_day,
        candidate_preceptor,
        candidate_location,
        ambulatory_week=ambulatory_week,
        available_clinics=available_clinics,
    )

    audit_record(
        actor=get_actor(),
        action="check_clinic_reassignment",
        reason=f"checked reassignment: {called_out_preceptor} called out {date_} {half_day} -> {candidate_preceptor} ({candidate_location})",
        details=json.dumps(
            {
                "called_out_preceptor": called_out_preceptor,
                "date": date_.isoformat(),
                "half_day": half_day,
                "candidate_preceptor": candidate_preceptor,
                "candidate_location": candidate_location,
                "is_clear": result.is_clear,
                "affected_residents": result.affected_residents,
                "findings": [{"rule": f.rule, "severity": f.severity, "message": f.message} for f in result.findings],
            }
        ),
    )

    if result.affected_residents:
        st.success(f"Affected resident(s): {', '.join(result.affected_residents)}")
    if result.is_clear:
        st.success("No blocking issues found.")
    else:
        st.error("This reassignment has at least one blocking issue — review before proceeding.")

    for finding in result.findings:
        container = st.error if finding.severity == "blocking" else st.warning
        container(finding.message)
