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

from real_schedule.common import (
    is_continuity_clinic_cell,
    is_inpatient_rotation,
    is_jeopardy_duty,
    is_jeopardy_label,
    is_non_committing_label,
    is_preceptor_cell,
    is_recognized_ambulatory_rotation,
)

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


# ---------------------------------------------------------------------------
# Tool 3: FSC day eligibility checker
# ---------------------------------------------------------------------------


_FSC_PORTION_COST = {"AM": 0.5, "PM": 0.5, "FULL": 1.0}
_FSC_HALVES_FOR_PORTION = {"AM": ("AM",), "PM": ("PM",), "FULL": ("AM", "PM")}


@dataclass(frozen=True)
class FscDayCheckResult:
    findings: list[CheckFinding] = field(default_factory=list)

    @property
    def is_clear(self) -> bool:
        return not any(f.severity == "blocking" for f in self.findings)


def _resident_week_rotation(resident_name: str, week_start: dt.date, master_schedule) -> str | None:
    for record in master_schedule:
        if record.resident_name == resident_name and record.week_start == week_start:
            return record.rotation
    return None


def _is_on_master_assist_list(resident_name: str, week_start: dt.date, master_assist) -> bool:
    """True if the resident has ANY Master Assist List entry for that week
    — any duty tag (PRIME/DOC/Pickett/JEOPARDY), not just jeopardy. Being
    listed on the Master Assist List at all that week means they're in the
    backup-duty pool (the program's own jeopardy-vs-assist-list
    distinction — see solver/repair.py) and can't be pulled from clinic."""
    return any(d.resident_name == resident_name and d.week_start == week_start for d in master_assist)


def _resident_day_part_cell(resident_name: str, date_: dt.date, half: str, ambulatory_week) -> str | None:
    for row in ambulatory_week:
        if row.resident_name == resident_name:
            return row.day_parts.get((date_, half))
    return None


def _resident_fsc_balance(resident_name: str, fsc_balances):
    for balance in fsc_balances:
        if balance.resident_name == resident_name:
            return balance
    return None


def check_fsc_day_request(
    resident_name: str,
    date_: dt.date,
    portion: str,
    *,
    master_schedule,
    master_assist,
    ambulatory_week,
    fsc_balances,
) -> FscDayCheckResult:
    """Validate a proposed FSC (Flexible Scholarly/Conference) day or
    half-day request: the resident must be on an ambulatory rotation that
    week, not on the assist list or jeopardy, and the specific day/half-day
    requested must not be their own continuity clinic (DOC/Pickett/PRIME).
    Also flags (never blocks) an insufficient FSC balance from FSCTracker.
    `portion` is "AM", "PM", or "FULL", matching check_clinic_reassignment's
    half_day vocabulary.
    """
    findings: list[CheckFinding] = []
    week_start = date_ - dt.timedelta(days=date_.weekday())

    rotation = _resident_week_rotation(resident_name, week_start, master_schedule)
    if rotation is None:
        findings.append(
            CheckFinding(
                rule="rotation_unknown",
                severity="warning",
                message=f"Could not find {resident_name}'s rotation for the week of {week_start} in the Master Schedule — confirm they're actually on ambulatory.",
                resident_name=resident_name,
                week_start=week_start,
            )
        )
    elif is_non_committing_label(rotation):
        findings.append(
            CheckFinding(
                rule="not_ambulatory",
                severity="blocking",
                message=f"{resident_name} is recorded as {rotation!r} the week of {week_start} — not an ambulatory week, can't approve an FSC day.",
                resident_name=resident_name,
                week_start=week_start,
            )
        )
    elif not is_recognized_ambulatory_rotation(rotation):
        if is_inpatient_rotation(rotation):
            findings.append(
                CheckFinding(
                    rule="not_ambulatory",
                    severity="blocking",
                    message=f"{resident_name} is on {rotation!r} the week of {week_start} — an inpatient rotation, can't be spared for a clinic day.",
                    resident_name=resident_name,
                    week_start=week_start,
                )
            )
        else:
            findings.append(
                CheckFinding(
                    rule="rotation_type_unconfirmed",
                    severity="warning",
                    message=f"Couldn't confirm {rotation!r} is an ambulatory rotation — verify manually before approving.",
                    resident_name=resident_name,
                    week_start=week_start,
                )
            )

    if _is_on_master_assist_list(resident_name, week_start, master_assist):
        findings.append(
            CheckFinding(
                rule="on_assist_list",
                severity="blocking",
                message=f"{resident_name} is on the assist/jeopardy list the week of {week_start} — can't be pulled from clinic.",
                resident_name=resident_name,
                week_start=week_start,
            )
        )

    cc_halves = [
        half
        for half in _FSC_HALVES_FOR_PORTION[portion]
        if is_continuity_clinic_cell(_resident_day_part_cell(resident_name, date_, half, ambulatory_week))
    ]
    if cc_halves:
        findings.append(
            CheckFinding(
                rule="own_continuity_clinic",
                severity="blocking",
                message=f"{resident_name} has their own continuity clinic ({', '.join(cc_halves)}) on {date_} — can't take an FSC day then.",
                resident_name=resident_name,
                week_start=date_,
            )
        )

    balance = _resident_fsc_balance(resident_name, fsc_balances)
    cost = _FSC_PORTION_COST[portion]
    if balance is None or balance.fsc_left is None:
        findings.append(
            CheckFinding(
                rule="fsc_balance_unknown",
                severity="warning",
                message=f"No FSC balance found for {resident_name} in the tracker — confirm eligibility manually.",
                resident_name=resident_name,
            )
        )
    elif balance.fsc_left < cost:
        findings.append(
            CheckFinding(
                rule="insufficient_fsc_balance",
                severity="warning",
                message=f"{resident_name} has {balance.fsc_left} FSC day(s) left; this request costs {cost} — may exceed their allotment (chief's call).",
                resident_name=resident_name,
            )
        )

    return FscDayCheckResult(findings=findings)
