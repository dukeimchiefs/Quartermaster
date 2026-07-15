"""Page 8 — Check Day Off Alignment.

Verifies a proposed "SAC" (Strategic Adjustment of Calendar) request per
the Well-being Policy: a resident asks for a specific day off during an
inpatient block (e.g. a wedding, aligning with a partner's schedule).
Checks whether the resident's currently-assigned team already has that
date off, and if not, which other teams on the same service/week do —
matching the policy's own example ("is it possible to be placed on a team
where Saturday XX is an off day?"). Read-only over the real workbooks at
Resident_Schedules/, same posture as the other pages — never writes back
to Excel or db.models, every check logged to audit_log.

Starts with a curated set of 5 confirmed-working service files (not all
~19 under weekly_INPATIENT_Schedules/ — the rest weren't verified against
this reader yet); expand _SERVICE_FILES as more are confirmed.
"""

from __future__ import annotations

import json
import os

import streamlit as st

from app.auth import get_actor, require_chief_auth
from audit.log import record as audit_record
from real_schedule.checks import check_day_off_alignment
from real_schedule.inpatient_schedule import load_inpatient_week_rows
from real_schedule.roster import RosterIndex, load_roster

require_chief_auth()

st.title("Check Day Off Alignment")
st.caption("Verify a proposed SAC (specific day-off) request against the real, live inpatient schedule (read-only).")

_RESIDENT_SCHEDULES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "Resident_Schedules")
_INPATIENT_DIR = os.path.join(_RESIDENT_SCHEDULES_DIR, "weekly_INPATIENT_Schedules")
_ROSTER_PATH = os.path.join(_RESIDENT_SCHEDULES_DIR, "duke_residency_2026-2027.csv")

_SERVICE_FILES = {
    "VA GM": (os.path.join(_INPATIENT_DIR, "VA", "VA GM", "VA GM 2026-2027.xlsx"), "Sheet1"),
    "DRH GM": (os.path.join(_INPATIENT_DIR, "DRH", "DRH GM 2026-27 Postable.xlsx"), "Sheet1"),
    "VA MICU": (os.path.join(_INPATIENT_DIR, "VA", "VA MICU", "VA MICU 2026-2027.xlsx"), "VA MICU Schedule"),
    "DUH MICU": (os.path.join(_INPATIENT_DIR, "DUH", "MICU", "DUH MICU 2026-27.xlsx"), "Sheet1"),
    "DUH CICU": (os.path.join(_INPATIENT_DIR, "DUH", "CICU", "DUKE CICU 2026-27.xlsx"), "Sheet1"),
}

if not os.path.isdir(_RESIDENT_SCHEDULES_DIR):
    st.error(f"Resident_Schedules/ not found at {_RESIDENT_SCHEDULES_DIR} — can't read the real schedules.")
    st.stop()

service_label = st.selectbox("Inpatient service", options=sorted(_SERVICE_FILES))
service_path, sheet_name = _SERVICE_FILES[service_label]


@st.cache_data(show_spinner="Reading real schedule workbooks...")
def _load_all(service_path: str, sheet_name: str, service_mtime: float, roster_mtime: float):
    del service_mtime, roster_mtime  # cache-key only
    roster_entries, w0 = load_roster(_ROSTER_PATH)
    roster = RosterIndex(roster_entries)
    inpatient_week_rows, w1 = load_inpatient_week_rows(service_path, sheet_name, roster=roster)
    return inpatient_week_rows, w0 + w1


try:
    mtimes = (os.path.getmtime(service_path), os.path.getmtime(_ROSTER_PATH))
except OSError as exc:
    st.error(f"Couldn't read one of the real schedule workbooks: {exc}")
    st.stop()

inpatient_week_rows, load_warnings = _load_all(service_path, sheet_name, *mtimes)

if load_warnings:
    with st.expander(f"{len(load_warnings)} parsing warning(s) while reading the real workbooks"):
        for w in load_warnings[:50]:
            st.caption(f"{w.sheet} (row {w.row}): {w.reason}")

resident_names = sorted({r.resident_name for r in inpatient_week_rows})
if not resident_names:
    st.warning("No residents could be parsed from this service's schedule.")
    st.stop()

resident_name = st.selectbox("Resident", options=resident_names)

candidate_dates = sorted({d for r in inpatient_week_rows if r.resident_name == resident_name for d in r.day_parts})
if not candidate_dates:
    st.warning(f"No dates found for {resident_name} on this service.")
    st.stop()

target_date = st.selectbox("Requested day off", options=candidate_dates, format_func=lambda d: d.strftime("%a, %b %-d, %Y"))

if st.button("Check this day-off request", type="primary"):
    result = check_day_off_alignment(
        resident_name,
        target_date,
        inpatient_week_rows=inpatient_week_rows,
    )

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
