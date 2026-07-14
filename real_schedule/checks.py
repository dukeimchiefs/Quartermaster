"""Swap/coverage validators over the real, live schedule data — the whole
point of real_schedule/. Both checkers are pure functions over already-
loaded reader output (no file I/O here); callers (Streamlit pages, the
manual verification script) load the workbooks once and pass records in.

Mirrors solver/rules.py's Violation-list shape conceptually (a flat list of
findings a caller renders), adapted to this package's name-keyed, no-DB
reality — this module does not import solver/rules.py; that module stays
reserved for the CP-SAT/DB-backed schedule builders.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from real_schedule.common import is_jeopardy_duty, is_jeopardy_label, is_non_committing_label, is_preceptor_cell

FAIRNESS_FLAG_THRESHOLD_PULLS = 2.0
"""A policy knob, not a derived "correct" number — flag (never block) a
swap if it would push the pull-count gap between the two residents beyond
this many pulls. The chief resident should adjust this to taste."""


@dataclass(frozen=True)
class CheckFinding:
    rule: str
    severity: str  # "blocking" | "warning"
    message: str
    resident_name: str | None = None
    week_start: dt.date | None = None


# ---------------------------------------------------------------------------
# Tool 1: assist/jeopardy week-swap checker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssistSwapCheckResult:
    findings: list[CheckFinding] = field(default_factory=list)
    reminders: list[str] = field(default_factory=list)

    @property
    def is_clear(self) -> bool:
        return not any(f.severity == "blocking" for f in self.findings)


_STANDARD_REMINDERS = (
    "Pull-list resident must be in town the entire week, reachable 24/7 (this isn't machine-checkable — confirm directly).",
    "Pull-list resident must not moonlight during their assist/jeopardy week (not machine-checkable).",
    "Keep the call-out reason confidential — never display or log it outside the audit log.",
)


def _resident_pgy_tier(resident_name: str, master_assist) -> str | None:
    for duty in master_assist:
        if duty.resident_name == resident_name:
            return duty.pgy_tier
    return None


def _is_on_jeopardy_or_assist(resident_name: str, week_start: dt.date, weekly_assist) -> bool:
    return any(
        e.resident_name == resident_name and e.week_start == week_start and is_jeopardy_label(e.rotation)
        for e in weekly_assist
    )


def _resident_commitment(resident_name: str, week_start: dt.date, master_schedule) -> str | None:
    for record in master_schedule:
        if record.resident_name == resident_name and record.week_start == week_start:
            return record.rotation
    return None


def check_assist_swap(
    resident_1: str,
    resident_2: str,
    week_covered: dt.date,
    week_new: dt.date,
    *,
    master_assist,
    weekly_assist,
    master_schedule,
    ambulatory_master=None,
) -> AssistSwapCheckResult:
    """Validate a proposed assist/jeopardy week swap: resident_2 covers
    resident_1's original week (`week_covered`); resident_1 gets a new
    week (`week_new`) instead. Inputs mirror the real Assist List Swaps
    sheet's own columns. `ambulatory_master` is accepted for forward
    compatibility (a future ambulatory-specific conflict check) but not
    yet consulted — master_schedule's rotation-per-week already carries
    enough signal for the conflicting-commitment checks below.
    """
    findings: list[CheckFinding] = []

    tier_1 = _resident_pgy_tier(resident_1, master_assist)
    tier_2 = _resident_pgy_tier(resident_2, master_assist)
    if tier_1 is not None and tier_2 is not None and tier_1 != tier_2:
        findings.append(
            CheckFinding(
                rule="pgy_mismatch",
                severity="blocking",
                message=f"{resident_1} is {tier_1} but {resident_2} is {tier_2} — a swap must be within the same PGY tier.",
            )
        )
    elif tier_1 is None or tier_2 is None:
        findings.append(
            CheckFinding(
                rule="pgy_tier_unknown",
                severity="warning",
                message="Could not determine PGY tier for one or both residents from the Master Assist List — confirm manually.",
            )
        )

    if not _is_on_jeopardy_or_assist(resident_1, week_covered, weekly_assist):
        findings.append(
            CheckFinding(
                rule="premise_mismatch",
                severity="blocking",
                message=f"{resident_1} isn't recorded as on jeopardy/assist for the week of {week_covered} — the proposed swap doesn't match the current schedule.",
                resident_name=resident_1,
                week_start=week_covered,
            )
        )

    commitment_2 = _resident_commitment(resident_2, week_covered, master_schedule)
    if commitment_2 is not None and not is_non_committing_label(commitment_2):
        findings.append(
            CheckFinding(
                rule="conflicting_commitment",
                severity="blocking",
                message=f"{resident_2} is already committed to {commitment_2!r} the week of {week_covered} and can't cover.",
                resident_name=resident_2,
                week_start=week_covered,
            )
        )

    commitment_1_new = _resident_commitment(resident_1, week_new, master_schedule)
    if commitment_1_new is not None and not is_non_committing_label(commitment_1_new):
        findings.append(
            CheckFinding(
                rule="conflicting_commitment",
                severity="blocking",
                message=f"{resident_1} is already committed to {commitment_1_new!r} the week of {week_new} — can't take that as their new assist/jeopardy week.",
                resident_name=resident_1,
                week_start=week_new,
            )
        )

    others_on_new_week = {
        e.resident_name
        for e in weekly_assist
        if is_jeopardy_label(e.rotation) and e.week_start == week_new and e.resident_name != resident_1
    }
    if others_on_new_week:
        findings.append(
            CheckFinding(
                rule="double_coverage",
                severity="warning",
                message=f"{', '.join(sorted(others_on_new_week))} already on jeopardy/assist the week of {week_new} too — allowed, but confirm that's intended.",
                week_start=week_new,
            )
        )

    findings.extend(_fairness_findings(resident_1, resident_2, master_assist))

    return AssistSwapCheckResult(findings=findings, reminders=list(_STANDARD_REMINDERS))


def _fairness_findings(resident_1: str, resident_2: str, master_assist) -> list[CheckFinding]:
    def total_pulls(name: str) -> int:
        return sum(1 for d in master_assist if d.resident_name == name and is_jeopardy_duty(d.duty, d.extra))

    count_1, count_2 = total_pulls(resident_1), total_pulls(resident_2)
    if abs(count_1 - count_2) >= FAIRNESS_FLAG_THRESHOLD_PULLS:
        return [
            CheckFinding(
                rule="fairness_flag",
                severity="warning",
                message=(
                    f"{resident_1} has {count_1} recorded jeopardy/assist weeks vs. {resident_2}'s {count_2} — "
                    f"this swap may widen an existing imbalance. Chiefs agree to spread assists as evenly as possible."
                ),
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Tool 2: preceptor call-out clinic-reassignment checker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClinicReassignmentCheckResult:
    findings: list[CheckFinding] = field(default_factory=list)
    affected_residents: list[str] = field(default_factory=list)

    @property
    def is_clear(self) -> bool:
        return not any(f.severity == "blocking" for f in self.findings)


def _weekday_half(date_: dt.date, half_day: str) -> tuple[str, str]:
    return date_.strftime("%a"), half_day


def check_clinic_reassignment(
    called_out_preceptor: str,
    date_: dt.date,
    half_day: str,
    candidate_preceptor: str,
    candidate_location: str,
    *,
    ambulatory_week,
    available_clinics,
) -> ClinicReassignmentCheckResult:
    findings: list[CheckFinding] = []

    affected: list[str] = []
    occupants_at_candidate: list[str] = []
    for row in ambulatory_week:
        cell = row.day_parts.get((date_, half_day))
        if cell is None:
            continue
        parsed = is_preceptor_cell(cell)
        if parsed is None:
            continue
        preceptor_name, site_code = parsed
        if preceptor_name == called_out_preceptor:
            affected.append(row.resident_name)
        if preceptor_name == candidate_preceptor and site_code == candidate_location:
            occupants_at_candidate.append(row.resident_name)

    if not affected:
        findings.append(
            CheckFinding(
                rule="no_affected_resident",
                severity="blocking",
                message=f"No resident is scheduled with {called_out_preceptor} on {date_} {half_day} — nothing to reassign.",
                week_start=date_,
            )
        )

    candidate_day_key = date_.strftime("%a")
    # available_day_parts stores full weekday names like "Mon" via the
    # source sheet's own day-part column labels ("Mon AM" -> ("Mon","AM"));
    # strftime("%a") on a Monday gives "Mon" too, so these align directly.
    matching_slot = next(
        (
            slot
            for slot in available_clinics
            if slot.preceptor_name == candidate_preceptor
            and (candidate_location is None or slot.location == candidate_location or candidate_location in slot.site_codes_by_day_part.values())
            and (candidate_day_key, half_day) in slot.available_day_parts
        ),
        None,
    )
    if matching_slot is None:
        findings.append(
            CheckFinding(
                rule="candidate_not_available",
                severity="blocking",
                message=f"{candidate_preceptor} ({candidate_location}) isn't listed as available {candidate_day_key} {half_day} in the Available Clinics list.",
                resident_name=candidate_preceptor,
                week_start=date_,
            )
        )
    elif matching_slot.notes:
        findings.append(
            CheckFinding(
                rule="blackout_note",
                severity="warning",
                message=f"{candidate_preceptor} has a note on file: {matching_slot.notes!r} — confirm this date isn't affected.",
                week_start=date_,
            )
        )

    if occupants_at_candidate:
        findings.append(
            CheckFinding(
                rule="possible_double_booking",
                severity="warning",
                message=(
                    f"{', '.join(occupants_at_candidate)} already placed with {candidate_preceptor} ({candidate_location}) "
                    f"that half-day — per-preceptor capacity isn't confirmed, so this may or may not be a real conflict."
                ),
                week_start=date_,
            )
        )

    if matching_slot is not None and affected:
        affected_tiers_known = matching_slot.tier
        findings.append(
            CheckFinding(
                rule="tier_fit_unconfirmed",
                severity="warning",
                message=(
                    f"Candidate slot is from '{affected_tiers_known}' — confirm the affected resident's own PGY level actually "
                    "fits that tier before placing them (not confirmed as a hard rule)."
                ),
            )
        )

    return ClinicReassignmentCheckResult(findings=findings, affected_residents=affected)
