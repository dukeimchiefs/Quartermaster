"""Page 6 — Check FSC/Reflection Day.

Verifies whether a resident can appropriately take a full day or half-day
FSC (Flexible Self Care) or Reflection day away from clinic, per the
Well-being Policy: are they on an eligible rotation that week (ambulatory,
consults, or SDE — NOT continuity clinic itself), are they free of
assist-list/jeopardy duty, is the specific day/half-day requested not their
own continuity clinic (DOC/Pickett/PRIME — "FSC/reflection time cannot
replace continuity clinic"), and do they have enough balance left in
FSCTracker. FSC and Reflection days are two separately-named entitlements
but share one request form, one eligibility rule, and one pooled tracker
balance, so they're checked here as a single combined concept, matching the
real "FSC/Reflection Request Form." Read-only over the real workbooks at
Resident_Schedules/, same posture as Pages 4/5 — never writes back to Excel
or db.models, every check logged to audit_log.
"""

from __future__ import annotations

import json
import os

import openpyxl
import streamlit as st

from app.auth import get_actor, require_chief_auth
from audit.log import record as audit_record
from real_schedule.ambulatory import load_ambulatory_week
from real_schedule.assist_list import load_master_assist_list
from real_schedule.checks import check_fsc_reflection_day_request
from real_schedule.fsc_tracker import load_fsc_tracker
from real_schedule.master_schedule import load_master_schedule
from real_schedule.roster import RosterIndex, load_roster

require_chief_auth()

st.title("Check FSC/Reflection Day")
st.caption("Verify a proposed FSC/Reflection day or half-day request against the real, live schedules (read-only).")

_RESIDENT_SCHEDULES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "Resident_Schedules")
_MASTER_SCHEDULE_PATH = os.path.join(_RESIDENT_SCHEDULES_DIR, "master_MASTER_Schedule_2026-2027.xlsx")
_MASTER_ASSIST_PATH = os.path.join(_RESIDENT_SCHEDULES_DIR, "master_ASSIST_List_2026-2027.xlsx")
_AMBULATORY_PATH = os.path.join(_RESIDENT_SCHEDULES_DIR, "master_AMBULATORY_schedule_2026-2027.xlsx")
_FSC_TRACKER_PATH = os.path.join(_RESIDENT_SCHEDULES_DIR, "FSC_ConfDay_Trackers_2026-2027.xlsx")
_ROSTER_PATH = os.path.join(_RESIDENT_SCHEDULES_DIR, "duke_residency_2026-2027.csv")
_NON_WEEK_SHEETS = ("Master", "Preferred Clinics", "Build")

if not os.path.isdir(_RESIDENT_SCHEDULES_DIR):
    st.error(f"Resident_Schedules/ not found at {_RESIDENT_SCHEDULES_DIR} — can't read the real schedules.")
    st.stop()

try:
    wb = openpyxl.load_workbook(_AMBULATORY_PATH, read_only=True)
    week_sheet_names = [name for name in wb.sheetnames if name not in _NON_WEEK_SHEETS]
except OSError as exc:
    st.error(f"Couldn't read the ambulatory schedule workbook: {exc}")
    st.stop()

week_sheet_name = st.selectbox("Week", options=week_sheet_names)


@st.cache_data(show_spinner="Reading real schedule workbooks...")
def _load_all(master_schedule_mtime: float, master_assist_mtime: float, fsc_tracker_mtime: float, ambulatory_mtime: float, roster_mtime: float, week_sheet_name: str):
    del master_schedule_mtime, master_assist_mtime, fsc_tracker_mtime, ambulatory_mtime, roster_mtime  # cache-key only
    roster_entries, w0 = load_roster(_ROSTER_PATH)
    roster = RosterIndex(roster_entries)
    master_schedule, w1 = load_master_schedule(_MASTER_SCHEDULE_PATH, roster=roster)
    master_assist, w2 = load_master_assist_list(_MASTER_ASSIST_PATH, roster=roster)
    fsc_balances, w3 = load_fsc_tracker(_FSC_TRACKER_PATH, roster=roster)
    ambulatory_week, w4 = load_ambulatory_week(_AMBULATORY_PATH, week_sheet_name, roster=roster)
    return master_schedule, master_assist, fsc_balances, ambulatory_week, w0 + w1 + w2 + w3 + w4


try:
    mtimes = tuple(
        os.path.getmtime(p) for p in (_MASTER_SCHEDULE_PATH, _MASTER_ASSIST_PATH, _FSC_TRACKER_PATH, _AMBULATORY_PATH, _ROSTER_PATH)
    )
except OSError as exc:
    st.error(f"Couldn't read one of the real schedule workbooks: {exc}")
    st.stop()

master_schedule, master_assist, fsc_balances, ambulatory_week, load_warnings = _load_all(*mtimes, week_sheet_name)

if load_warnings:
    with st.expander(f"{len(load_warnings)} parsing warning(s) while reading the real workbooks"):
        for w in load_warnings[:50]:
            st.caption(f"{w.sheet} (row {w.row}): {w.reason}")

resident_names = sorted({r.resident_name for r in master_schedule})
if not resident_names:
    st.warning("No residents could be parsed from the Master Schedule.")
    st.stop()

resident_name = st.selectbox("Resident", options=resident_names)

col1, col2 = st.columns(2)
with col1:
    weekday_names = ["Mon", "Tues", "Wed", "Thurs", "Fri"]
    weekday = st.selectbox("Day", options=weekday_names)
with col2:
    portion_label = st.radio("Portion", options=["Half-day AM", "Half-day PM", "Full day"], horizontal=True)
portion = {"Half-day AM": "AM", "Half-day PM": "PM", "Full day": "FULL"}[portion_label]

# Resolve the actual date for the chosen weekday from this week's parsed rows.
candidate_dates = sorted({d for row in ambulatory_week for (d, h) in row.day_parts})
weekday_index = weekday_names.index(weekday)
matching_dates = [d for d in candidate_dates if d.weekday() == weekday_index]
if not matching_dates:
    st.warning(f"No {weekday} date found in this week's ambulatory sheet.")
    st.stop()
date_ = matching_dates[0]

if st.button("Check this FSC/Reflection request", type="primary"):
    result = check_fsc_reflection_day_request(
        resident_name,
        date_,
        portion,
        master_schedule=master_schedule,
        master_assist=master_assist,
        ambulatory_week=ambulatory_week,
        fsc_balances=fsc_balances,
    )

    audit_record(
        actor=get_actor(),
        action="check_fsc_reflection_day_request",
        reason=f"checked FSC/Reflection request: {resident_name} on {date_} ({portion_label})",
        details=json.dumps(
            {
                "resident_name": resident_name,
                "date": date_.isoformat(),
                "portion": portion,
                "is_clear": result.is_clear,
                "findings": [{"rule": f.rule, "severity": f.severity, "message": f.message} for f in result.findings],
            }
        ),
    )

    if result.is_clear:
        st.success("No blocking issues found.")
    else:
        st.error("This FSC/Reflection request has at least one blocking issue — review before proceeding.")

    for finding in result.findings:
        container = st.error if finding.severity == "blocking" else st.warning
        container(finding.message)

    st.divider()
    st.caption("Reminders (not machine-checkable):")
    for reminder in result.reminders:
        st.info(reminder)
