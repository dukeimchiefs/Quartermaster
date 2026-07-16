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

Has two ways to run a check — the structured form and a free-text box
parsed by the local assistant (llm.tools.handle_check_fsc_reflection_message,
resolved within the currently-selected week's already-loaded ambulatory
data — pick a different week from the dropdown first) — both converge on
the same check_fsc_reflection_day_request() result and the same
render/review section below, stored in st.session_state for the same
Streamlit-rerun reason as the other Check pages.

Nested "Review": since this page is read-only over Excel by design, review
means recording an Approve/Reject decision to audit_log — the chief still
has to update the real FSC/Reflection Request record by hand.
"""

from __future__ import annotations

import datetime as dt
import json
import os

import openpyxl
import streamlit as st

from app.auth import get_actor, require_chief_auth
from audit.log import record as audit_record
from llm.tools import handle_check_fsc_reflection_message
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


def _result_to_dict(result, *, inputs: dict, source: str) -> dict:
    return {
        "source": source,
        "inputs": inputs,
        "is_clear": result.is_clear,
        "findings": [{"rule": f.rule, "severity": f.severity, "message": f.message} for f in result.findings],
        "reminders": list(result.reminders),
    }


def _stage_result(result_dict: dict) -> None:
    st.session_state["fsc_reflection_result"] = result_dict
    st.session_state.pop("fsc_reflection_review_status", None)


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
        resident_name, date_, portion,
        master_schedule=master_schedule, master_assist=master_assist,
        ambulatory_week=ambulatory_week, fsc_balances=fsc_balances,
    )
    inputs = {"resident_name": resident_name, "date": date_.isoformat(), "portion": portion}
    audit_record(
        actor=get_actor(),
        action="check_fsc_reflection_day_request",
        reason=f"checked FSC/Reflection request: {resident_name} on {date_} ({portion_label})",
        details=json.dumps({**inputs, "is_clear": result.is_clear, "findings": [
            {"rule": f.rule, "severity": f.severity, "message": f.message} for f in result.findings
        ]}),
    )
    _stage_result(_result_to_dict(result, inputs=inputs, source="structured"))

st.divider()
st.subheader("Or describe it in your own words")
free_text = st.text_area(
    "What's the request?", placeholder='e.g. "Can Jordan take Thursday afternoon off for reflection day?"',
    key="fsc_reflection_free_text",
)
if st.button("Parse & check"):
    if not free_text.strip():
        st.warning("Enter a description first.")
    else:
        try:
            with st.spinner("Asking the local assistant..."):
                handled = handle_check_fsc_reflection_message(
                    master_schedule, master_assist, ambulatory_week, fsc_balances, free_text, today=dt.date.today()
                )
        except Exception as exc:  # Ollama not running, model not pulled, etc.
            st.error(f"Local assistant unavailable ({exc}). Use the structured form above instead.")
        else:
            if not handled.resolved:
                st.info(handled.reply)
            else:
                result = handled.result
                inputs = {
                    "resident_name": handled.resolved_args["resident"],
                    "date": handled.resolved_args["date"].isoformat(),
                    "portion": handled.resolved_args["portion"],
                    "free_text": free_text.strip(),
                }
                audit_record(
                    actor=get_actor(),
                    action="check_fsc_reflection_day_request",
                    reason=f'checked FSC/Reflection request (free text): "{free_text.strip()}"',
                    details=json.dumps({**inputs, "is_clear": result.is_clear, "findings": [
                        {"rule": f.rule, "severity": f.severity, "message": f.message} for f in result.findings
                    ]}),
                )
                result_dict = _result_to_dict(result, inputs=inputs, source="free_text")
                result_dict["narration"] = handled.reply
                _stage_result(result_dict)

stored = st.session_state.get("fsc_reflection_result")
if stored:
    st.divider()
    if stored["source"] == "free_text":
        st.success(stored["narration"])
    if stored["is_clear"]:
        st.success("No blocking issues found.")
    else:
        st.error("This FSC/Reflection request has at least one blocking issue — review before proceeding.")

    for finding in stored["findings"]:
        container = st.error if finding["severity"] == "blocking" else st.warning
        container(finding["message"])

    if stored["reminders"]:
        st.divider()
        st.caption("Reminders (not machine-checkable):")
        for reminder in stored["reminders"]:
            st.info(reminder)

    st.divider()
    st.subheader("Review")
    review_status = st.session_state.get("fsc_reflection_review_status")
    if review_status:
        st.info(f"Already recorded as **{review_status}**.")
    else:
        st.caption(
            "This app never writes to Resident_Schedules/ — approving here only records the chief's decision "
            "to the audit log. Update the real FSC/Reflection Request record yourself."
        )
        rev_col1, rev_col2 = st.columns(2)
        with rev_col1:
            approve = st.button("Approve", type="primary", key="approve_fsc_reflection_request")
        with rev_col2:
            reject = st.button("Reject", key="reject_fsc_reflection_request")
        if approve:
            audit_record(
                actor=get_actor(), action="approve_fsc_reflection_request",
                reason=f"approved FSC/Reflection request: {stored['inputs']}", details=json.dumps(stored),
            )
            st.session_state["fsc_reflection_review_status"] = "approved"
            st.rerun()
        if reject:
            audit_record(
                actor=get_actor(), action="reject_fsc_reflection_request",
                reason=f"rejected FSC/Reflection request: {stored['inputs']}", details=json.dumps(stored),
            )
            st.session_state["fsc_reflection_review_status"] = "rejected"
            st.rerun()
