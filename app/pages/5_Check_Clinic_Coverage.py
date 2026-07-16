"""Page 5 — Check Clinic Coverage.

Finds and verifies candidate reassignments when an ambulatory preceptor
calls out: which resident(s) are actually affected, which other same-
specialty preceptors are available that half-day, and whether placing a
resident there is otherwise clean (no existing double-booking, tier fit
confirmed manually). Read-only over the real workbooks at
Resident_Schedules/, same posture as Page 4 — never writes back to Excel or
db.models, every check logged to audit_log.

Candidates are proposed automatically (real_schedule.recommend.
recommend_clinic_coverage) rather than requiring the chief to enter a
candidate preceptor and location by hand: restricted to the SAME specialty
as the preceptor who called out, ranked with a "keep it local" site-group
heuristic first (see that module's docstring — no real distance/campus
field exists in the data, so this is a best-effort proxy, not a verified
geographic calculation).

Has two ways to find candidates — the structured form (pick who called
out + day/half-day) and a free-text box parsed by the local assistant
(llm.tools.handle_check_clinic_coverage_message, resolved within the
currently-selected week's already-loaded ambulatory data) — both produce a
ranked candidate list rendered as cards; choosing one runs the same
check_clinic_reassignment() the old manual form used and stages it for the
review section below. Everything is stored in st.session_state rather than
rendered inside a button's if-block, same Streamlit-rerun reason as the
other Check pages.

The "available clinics" file is seasonal (Fall 2026 now, a Spring version
later) — this page globs for it rather than hardcoding the filename.

Nested "Review": since this page is read-only over Excel by design, review
means recording an Approve/Reject decision to audit_log — the chief still
has to update the real ambulatory schedule / Epic record by hand.
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
from llm.tools import handle_check_clinic_coverage_message
from real_schedule.ambulatory import load_ambulatory_week
from real_schedule.available_clinics import load_available_clinics
from real_schedule.checks import check_clinic_reassignment
from real_schedule.common import is_preceptor_cell
from real_schedule.recommend import recommend_clinic_coverage
from real_schedule.roster import RosterIndex, load_roster

require_chief_auth()

st.title("Check Clinic Coverage")
st.caption("Find and verify a candidate reassignment after an ambulatory preceptor calls out (read-only).")

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

if not preceptor_names:
    st.warning("No preceptor-attributed clinic sessions found in this week's ambulatory sheet.")
    st.stop()


def _candidates_to_dicts(candidates) -> list[dict]:
    return [
        {
            "rank": c.rank, "preceptor_name": c.preceptor_name, "location": c.location, "specialty": c.specialty,
            "is_clear": c.is_clear, "same_site_group": c.same_site_group,
            "findings": [{"rule": f.rule, "severity": f.severity, "message": f.message} for f in c.findings],
        }
        for c in candidates
    ]


def _stage_candidates(payload: dict) -> None:
    st.session_state["clinic_coverage_candidates"] = payload
    st.session_state.pop("clinic_coverage_result", None)
    st.session_state.pop("clinic_coverage_review_status", None)


def _stage_chosen(*, inputs: dict, source: str) -> None:
    result = check_clinic_reassignment(
        inputs["called_out_preceptor"], dt.date.fromisoformat(inputs["date"]), inputs["half_day"],
        inputs["candidate_preceptor"], inputs["candidate_location"],
        ambulatory_week=ambulatory_week, available_clinics=available_clinics,
    )
    audit_record(
        actor=get_actor(),
        action="check_clinic_reassignment",
        reason=f"checked reassignment: {inputs['called_out_preceptor']} called out {inputs['date']} {inputs['half_day']} -> {inputs['candidate_preceptor']} ({inputs['candidate_location']})",
        details=json.dumps({**inputs, "is_clear": result.is_clear, "affected_residents": result.affected_residents, "findings": [
            {"rule": f.rule, "severity": f.severity, "message": f.message} for f in result.findings
        ]}),
    )
    st.session_state["clinic_coverage_result"] = {
        "source": source, "inputs": inputs, "is_clear": result.is_clear,
        "affected_residents": result.affected_residents,
        "findings": [{"rule": f.rule, "severity": f.severity, "message": f.message} for f in result.findings],
    }
    st.session_state.pop("clinic_coverage_review_status", None)


def _render_candidate_cards(payload: dict) -> None:
    candidates = payload["candidates"]
    if not candidates:
        st.error(f"No same-specialty candidate found for {payload['called_out_preceptor']} on {payload['date']} {payload['half_day']}.")
        return
    st.success(f"Found {len(candidates)} candidate(s), ranked (site-local first, then clean checks).")
    for candidate in candidates:
        with st.container(border=True):
            badge = "✅" if candidate["is_clear"] else "⚠️"
            local_tag = " · local" if candidate["same_site_group"] else ""
            st.markdown(f"**#{candidate['rank']} — {candidate['preceptor_name']}** ({candidate['location']}){local_tag} {badge}")
            for finding in candidate["findings"]:
                container = st.error if finding["severity"] == "blocking" else st.warning
                container(finding["message"])
            if st.button(
                f"Choose {candidate['preceptor_name']} ({candidate['location']})",
                key=f"choose_clinic_{payload['source']}_{candidate['rank']}_{candidate['preceptor_name']}",
            ):
                _stage_chosen(
                    inputs={
                        "called_out_preceptor": payload["called_out_preceptor"], "date": payload["date"],
                        "half_day": payload["half_day"], "candidate_preceptor": candidate["preceptor_name"],
                        "candidate_location": candidate["location"],
                    },
                    source=payload["source"],
                )


called_out_preceptor = st.selectbox("Preceptor who called out", options=preceptor_names)

col1, col2 = st.columns(2)
with col1:
    weekday_names = ["Mon", "Tues", "Wed", "Thurs", "Fri"]
    weekday = st.selectbox("Day", options=weekday_names)
with col2:
    half_day = st.selectbox("Half-day", options=["AM", "PM"])

candidate_dates = sorted({d for row in ambulatory_week for (d, h) in row.day_parts if h == half_day})
weekday_index = weekday_names.index(weekday)
matching_dates = [d for d in candidate_dates if d.weekday() == weekday_index]
if not matching_dates:
    st.warning(f"No {weekday} date found in this week's sheet.")
    st.stop()
date_ = matching_dates[0]

if st.button("Find candidates", type="primary"):
    candidates = recommend_clinic_coverage(
        called_out_preceptor, date_, half_day, ambulatory_week=ambulatory_week, available_clinics=available_clinics
    )
    _stage_candidates(
        {
            "source": "structured", "called_out_preceptor": called_out_preceptor,
            "date": date_.isoformat(), "half_day": half_day, "candidates": _candidates_to_dicts(candidates),
        }
    )

st.divider()
st.subheader("Or describe it in your own words")
st.caption(
    "Parses free text into the called-out preceptor, date, and half-day via the local assistant, resolved "
    "within this week's already-loaded ambulatory schedule (pick a different week above first if needed) — "
    "then always runs the same real_schedule.recommend.recommend_clinic_coverage() as the form above."
)
free_text = st.text_area(
    "What happened?", placeholder='e.g. "Dr. Burns is out Wednesday PM, need someone to cover"', key="clinic_coverage_free_text"
)
if st.button("Parse & find candidates"):
    if not free_text.strip():
        st.warning("Enter a description first.")
    else:
        try:
            with st.spinner("Asking the local assistant..."):
                handled = handle_check_clinic_coverage_message(
                    ambulatory_week, available_clinics, free_text, today=dt.date.today()
                )
        except Exception as exc:  # Ollama not running, model not pulled, etc.
            st.error(f"Local assistant unavailable ({exc}). Use the structured form above instead.")
        else:
            if not handled.resolved:
                st.info(handled.reply)
            else:
                _stage_candidates(
                    {
                        "source": "free_text",
                        "called_out_preceptor": handled.resolved_args["called_out_preceptor"],
                        "date": handled.resolved_args["date"].isoformat(),
                        "half_day": handled.resolved_args["half_day"],
                        "candidates": _candidates_to_dicts(handled.result),
                        "narration": handled.reply,
                    }
                )

candidates_payload = st.session_state.get("clinic_coverage_candidates")
if candidates_payload:
    st.divider()
    if candidates_payload["source"] == "free_text":
        st.success(candidates_payload["narration"])
    _render_candidate_cards(candidates_payload)

chosen = st.session_state.get("clinic_coverage_result")
if chosen:
    st.divider()
    if chosen["affected_residents"]:
        st.success(f"Affected resident(s): {', '.join(chosen['affected_residents'])}")
    if chosen["is_clear"]:
        st.success("No blocking issues found.")
    else:
        st.error("This reassignment has at least one blocking issue — review before proceeding.")
    for finding in chosen["findings"]:
        container = st.error if finding["severity"] == "blocking" else st.warning
        container(finding["message"])

    st.divider()
    st.subheader("Review")
    review_status = st.session_state.get("clinic_coverage_review_status")
    if review_status:
        st.info(f"Already recorded as **{review_status}**.")
    else:
        st.caption(
            "This app never writes to Resident_Schedules/ — approving here only records the chief's decision "
            "to the audit log. Update the real ambulatory schedule / Epic record yourself."
        )
        rev_col1, rev_col2 = st.columns(2)
        with rev_col1:
            approve = st.button("Approve", type="primary", key="approve_clinic_reassignment")
        with rev_col2:
            reject = st.button("Reject", key="reject_clinic_reassignment")
        if approve:
            audit_record(
                actor=get_actor(), action="approve_clinic_reassignment",
                reason=f"approved clinic reassignment: {chosen['inputs']}", details=json.dumps(chosen),
            )
            st.session_state["clinic_coverage_review_status"] = "approved"
            st.rerun()
        if reject:
            audit_record(
                actor=get_actor(), action="reject_clinic_reassignment",
                reason=f"rejected clinic reassignment: {chosen['inputs']}", details=json.dumps(chosen),
            )
            st.session_state["clinic_coverage_review_status"] = "rejected"
            st.rerun()
