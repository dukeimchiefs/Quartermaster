"""Tests for real_schedule/checks.py — the actual swap/coverage validators.
Small, hand-built in-memory fixtures (fictional names), matching
tests/test_repair.py's existing pattern of duck-typed dataclasses rather
than full workbook round-trips for pure-logic tests.
"""

from __future__ import annotations

import datetime as dt

from real_schedule.assist_list import AssistWeekEntry, MasterAssistDuty
from real_schedule.ambulatory import AmbulatoryWeekRow
from real_schedule.available_clinics import ClinicSlot
from real_schedule.checks import (
    FAIRNESS_FLAG_THRESHOLD_PULLS,
    check_assist_swap,
    check_clinic_reassignment,
    check_day_off_alignment,
    check_fsc_reflection_day_request,
    check_rotation_swap,
)
from real_schedule.fsc_tracker import FscBalance
from real_schedule.inpatient_schedule import InpatientDayRow
from real_schedule.master_schedule import MasterScheduleWeek

WEEK_COVERED = dt.date(2026, 7, 6)
WEEK_NEW = dt.date(2026, 8, 3)


def _base_inputs():
    master_assist = [
        MasterAssistDuty(resident_name="Alpha, Fictional", pgy_tier="PGY-2", week_start=WEEK_COVERED, duty="JEOPARDY", extra=None),
        MasterAssistDuty(resident_name="Beta, Fictional", pgy_tier="PGY-2", week_start=WEEK_COVERED, duty="", extra=None),
    ]
    weekly_assist = [
        AssistWeekEntry(
            resident_name="Alpha, Fictional", pgy=2, rotation="JEOPARDY", week_start=WEEK_COVERED,
            pulls_this_year=1.0, day_parts={WEEK_COVERED: "Jeopardy"},
        ),
    ]
    master_schedule = [
        MasterScheduleWeek(resident_name="Beta, Fictional", pgy=2, week_start=WEEK_COVERED, rotation="OFF"),
        MasterScheduleWeek(resident_name="Alpha, Fictional", pgy=2, week_start=WEEK_NEW, rotation="OFF"),
    ]
    return master_assist, weekly_assist, master_schedule


def test_clean_swap_has_no_blocking_findings():
    master_assist, weekly_assist, master_schedule = _base_inputs()
    result = check_assist_swap(
        "Alpha, Fictional", "Beta, Fictional", WEEK_COVERED, WEEK_NEW,
        master_assist=master_assist, weekly_assist=weekly_assist, master_schedule=master_schedule,
    )
    assert result.is_clear
    assert len(result.reminders) == 3


def test_pgy_mismatch_is_blocking():
    master_assist, weekly_assist, master_schedule = _base_inputs()
    master_assist = [
        MasterAssistDuty(resident_name="Alpha, Fictional", pgy_tier="PGY-2", week_start=WEEK_COVERED, duty="JEOPARDY", extra=None),
        MasterAssistDuty(resident_name="Beta, Fictional", pgy_tier="PGY-3 +", week_start=WEEK_COVERED, duty="", extra=None),
    ]
    result = check_assist_swap(
        "Alpha, Fictional", "Beta, Fictional", WEEK_COVERED, WEEK_NEW,
        master_assist=master_assist, weekly_assist=weekly_assist, master_schedule=master_schedule,
    )
    assert not result.is_clear
    assert any(f.rule == "pgy_mismatch" and f.severity == "blocking" for f in result.findings)


def test_premise_mismatch_when_resident_1_not_actually_on_jeopardy():
    master_assist, _, master_schedule = _base_inputs()
    weekly_assist = []  # nobody recorded as on jeopardy that week at all
    result = check_assist_swap(
        "Alpha, Fictional", "Beta, Fictional", WEEK_COVERED, WEEK_NEW,
        master_assist=master_assist, weekly_assist=weekly_assist, master_schedule=master_schedule,
    )
    assert not result.is_clear
    assert any(f.rule == "premise_mismatch" for f in result.findings)


def test_resident_2_conflicting_commitment_blocks():
    master_assist, weekly_assist, _ = _base_inputs()
    master_schedule = [
        MasterScheduleWeek(resident_name="Beta, Fictional", pgy=2, week_start=WEEK_COVERED, rotation="VA GM"),
    ]
    result = check_assist_swap(
        "Alpha, Fictional", "Beta, Fictional", WEEK_COVERED, WEEK_NEW,
        master_assist=master_assist, weekly_assist=weekly_assist, master_schedule=master_schedule,
    )
    assert not result.is_clear
    finding = next(f for f in result.findings if f.rule == "conflicting_commitment")
    assert finding.resident_name == "Beta, Fictional"
    assert finding.week_start == WEEK_COVERED


def test_resident_1_new_week_conflicting_commitment_blocks():
    master_assist, weekly_assist, _ = _base_inputs()
    master_schedule = [
        MasterScheduleWeek(resident_name="Beta, Fictional", pgy=2, week_start=WEEK_COVERED, rotation="OFF"),
        MasterScheduleWeek(resident_name="Alpha, Fictional", pgy=2, week_start=WEEK_NEW, rotation="AMB Endo"),
    ]
    result = check_assist_swap(
        "Alpha, Fictional", "Beta, Fictional", WEEK_COVERED, WEEK_NEW,
        master_assist=master_assist, weekly_assist=weekly_assist, master_schedule=master_schedule,
    )
    assert not result.is_clear
    finding = next(f for f in result.findings if f.rule == "conflicting_commitment" and f.resident_name == "Alpha, Fictional")
    assert finding.week_start == WEEK_NEW


def test_jeopardy_commitment_is_not_treated_as_conflicting():
    """Regression test for the real bug caught via retroactive smoke test:
    a resident's own upcoming jeopardy assignment must not be flagged as a
    'conflicting commitment' just because it's a non-blank rotation cell."""
    master_assist, weekly_assist, _ = _base_inputs()
    master_schedule = [
        MasterScheduleWeek(resident_name="Beta, Fictional", pgy=2, week_start=WEEK_COVERED, rotation="OFF"),
        MasterScheduleWeek(resident_name="Alpha, Fictional", pgy=2, week_start=WEEK_NEW, rotation="JEOPARDY- I"),
    ]
    result = check_assist_swap(
        "Alpha, Fictional", "Beta, Fictional", WEEK_COVERED, WEEK_NEW,
        master_assist=master_assist, weekly_assist=weekly_assist, master_schedule=master_schedule,
    )
    assert not any(f.rule == "conflicting_commitment" for f in result.findings)


def test_double_coverage_is_a_warning_not_blocking():
    master_assist, weekly_assist, master_schedule = _base_inputs()
    weekly_assist = weekly_assist + [
        AssistWeekEntry(
            resident_name="Gamma, Fictional", pgy=2, rotation="JEOPARDY", week_start=WEEK_NEW,
            pulls_this_year=0.0, day_parts={WEEK_NEW: "Jeopardy"},
        ),
    ]
    result = check_assist_swap(
        "Alpha, Fictional", "Beta, Fictional", WEEK_COVERED, WEEK_NEW,
        master_assist=master_assist, weekly_assist=weekly_assist, master_schedule=master_schedule,
    )
    finding = next(f for f in result.findings if f.rule == "double_coverage")
    assert finding.severity == "warning"
    assert result.is_clear  # a warning alone doesn't block


def test_fairness_flag_when_pull_counts_diverge():
    master_assist = [
        MasterAssistDuty(resident_name="Alpha, Fictional", pgy_tier="PGY-2", week_start=WEEK_COVERED, duty="JEOPARDY", extra=None),
        MasterAssistDuty(resident_name="Beta, Fictional", pgy_tier="PGY-2", week_start=WEEK_COVERED, duty="", extra=None),
    ]
    # Alpha has many more recorded jeopardy weeks than Beta.
    for i in range(int(FAIRNESS_FLAG_THRESHOLD_PULLS) + 2):
        master_assist.append(
            MasterAssistDuty(resident_name="Alpha, Fictional", pgy_tier="PGY-2", week_start=WEEK_COVERED + dt.timedelta(weeks=i + 1), duty="JEOPARDY", extra=None)
        )
    weekly_assist = [
        AssistWeekEntry(resident_name="Alpha, Fictional", pgy=2, rotation="JEOPARDY", week_start=WEEK_COVERED, pulls_this_year=1.0, day_parts={WEEK_COVERED: "Jeopardy"}),
    ]
    master_schedule = [
        MasterScheduleWeek(resident_name="Beta, Fictional", pgy=2, week_start=WEEK_COVERED, rotation="OFF"),
        MasterScheduleWeek(resident_name="Alpha, Fictional", pgy=2, week_start=WEEK_NEW, rotation="OFF"),
    ]
    result = check_assist_swap(
        "Alpha, Fictional", "Beta, Fictional", WEEK_COVERED, WEEK_NEW,
        master_assist=master_assist, weekly_assist=weekly_assist, master_schedule=master_schedule,
    )
    finding = next(f for f in result.findings if f.rule == "fairness_flag")
    assert finding.severity == "warning"


# --- Tool 2: check_clinic_reassignment --------------------------------------


def test_clinic_reassignment_finds_affected_resident_and_valid_candidate():
    date_ = dt.date(2026, 7, 6)
    ambulatory_week = [
        AmbulatoryWeekRow(
            resident_name="Delta, Fictional", pgy=1, rotation="AMB Endo",
            day_parts={(date_, "AM"): "Dr. Called Out\n(SD)"},
        ),
    ]
    available_clinics = [
        ClinicSlot(
            preceptor_name="Dr. Available", specialty="ID", location="SD", tier="Intern Blocks",
            available_day_parts={("Mon", "AM")}, site_codes_by_day_part={("Mon", "AM"): "SD"},
        ),
    ]
    result = check_clinic_reassignment(
        "Dr. Called Out", date_, "AM", "Dr. Available", "SD",
        ambulatory_week=ambulatory_week, available_clinics=available_clinics,
    )
    assert result.is_clear
    assert result.affected_residents == ["Delta, Fictional"]


def test_clinic_reassignment_blocks_when_nobody_affected():
    date_ = dt.date(2026, 7, 6)
    ambulatory_week = [
        AmbulatoryWeekRow(resident_name="Delta, Fictional", pgy=1, rotation="AMB Endo", day_parts={}),
    ]
    available_clinics = [
        ClinicSlot(preceptor_name="Dr. Available", specialty="ID", location="SD", tier="Intern Blocks", available_day_parts={("Mon", "AM")}),
    ]
    result = check_clinic_reassignment(
        "Dr. Called Out", date_, "AM", "Dr. Available", "SD",
        ambulatory_week=ambulatory_week, available_clinics=available_clinics,
    )
    assert not result.is_clear
    assert result.affected_residents == []
    assert any(f.rule == "no_affected_resident" for f in result.findings)


def test_clinic_reassignment_blocks_when_candidate_not_available_that_half_day():
    date_ = dt.date(2026, 7, 6)
    ambulatory_week = [
        AmbulatoryWeekRow(resident_name="Delta, Fictional", pgy=1, rotation="AMB Endo", day_parts={(date_, "AM"): "Dr. Called Out\n(SD)"}),
    ]
    available_clinics = [
        # only available Monday PM, not AM
        ClinicSlot(preceptor_name="Dr. Available", specialty="ID", location="SD", tier="Intern Blocks", available_day_parts={("Mon", "PM")}),
    ]
    result = check_clinic_reassignment(
        "Dr. Called Out", date_, "AM", "Dr. Available", "SD",
        ambulatory_week=ambulatory_week, available_clinics=available_clinics,
    )
    assert not result.is_clear
    assert any(f.rule == "candidate_not_available" for f in result.findings)


def test_clinic_reassignment_cc_panel_cells_never_count_as_affected():
    """The core disambiguation risk: a bare CC-panel placeholder (no
    preceptor name) must never be treated as "affected by preceptor X,"
    even if its text happens to contain X's name as a substring somehow."""
    date_ = dt.date(2026, 7, 6)
    ambulatory_week = [
        AmbulatoryWeekRow(resident_name="Delta, Fictional", pgy=1, rotation="AMB Endo", day_parts={(date_, "AM"): "DOC"}),
    ]
    available_clinics = [
        ClinicSlot(preceptor_name="Dr. Available", specialty="ID", location="SD", tier="Intern Blocks", available_day_parts={("Mon", "AM")}),
    ]
    result = check_clinic_reassignment(
        "DOC", date_, "AM", "Dr. Available", "SD",
        ambulatory_week=ambulatory_week, available_clinics=available_clinics,
    )
    assert result.affected_residents == []


def test_clinic_reassignment_warns_on_possible_double_booking():
    date_ = dt.date(2026, 7, 6)
    ambulatory_week = [
        AmbulatoryWeekRow(resident_name="Delta, Fictional", pgy=1, rotation="AMB Endo", day_parts={(date_, "AM"): "Dr. Called Out\n(SD)"}),
        AmbulatoryWeekRow(resident_name="Epsilon, Fictional", pgy=1, rotation="AMB ID", day_parts={(date_, "AM"): "Dr. Available\n(SD)"}),
    ]
    available_clinics = [
        ClinicSlot(preceptor_name="Dr. Available", specialty="ID", location="SD", tier="Intern Blocks", available_day_parts={("Mon", "AM")}),
    ]
    result = check_clinic_reassignment(
        "Dr. Called Out", date_, "AM", "Dr. Available", "SD",
        ambulatory_week=ambulatory_week, available_clinics=available_clinics,
    )
    finding = next(f for f in result.findings if f.rule == "possible_double_booking")
    assert finding.severity == "warning"


# --- Tool 3: check_fsc_reflection_day_request -------------------------------

_FSC_DATE = dt.date(2026, 7, 6)  # a Monday
_FSC_WEEK_START = dt.date(2026, 7, 6)


def _fsc_base_inputs():
    master_schedule = [
        MasterScheduleWeek(resident_name="Zeta, Fictional", pgy=1, week_start=_FSC_WEEK_START, rotation="AMB Endo"),
    ]
    master_assist: list[MasterAssistDuty] = []
    ambulatory_week = [
        AmbulatoryWeekRow(
            resident_name="Zeta, Fictional", pgy=1, rotation="AMB Endo",
            day_parts={(_FSC_DATE, "AM"): "Dr. Preceptor\n(SD)", (_FSC_DATE, "PM"): "AHD"},
        ),
    ]
    fsc_balances = [
        FscBalance(resident_name="Zeta, Fictional", pgy=1, program="Categorical", base_fsc=4.0, fsc_available=4.0, fsc_used=0.0, fsc_left=4.0, phase="Appointment Time"),
    ]
    return master_schedule, master_assist, ambulatory_week, fsc_balances


def test_clean_fsc_request_has_no_blocking_findings():
    master_schedule, master_assist, ambulatory_week, fsc_balances = _fsc_base_inputs()
    result = check_fsc_reflection_day_request(
        "Zeta, Fictional", _FSC_DATE, "AM",
        master_schedule=master_schedule, master_assist=master_assist,
        ambulatory_week=ambulatory_week, fsc_balances=fsc_balances,
    )
    assert result.is_clear
    assert len(result.reminders) == 2


def test_bare_consult_service_rotation_is_eligible():
    """Per the Well-being Policy, "ambulatory, consults, and SDE" rotations
    are FSC/Reflection-eligible — a bare specialty name like "GI" (no AMB/CS
    prefix) is this program's convention for that specialty's consult
    service, confirmed by the chief resident."""
    master_schedule, master_assist, ambulatory_week, fsc_balances = _fsc_base_inputs()
    master_schedule = [MasterScheduleWeek(resident_name="Zeta, Fictional", pgy=1, week_start=_FSC_WEEK_START, rotation="GI")]
    result = check_fsc_reflection_day_request(
        "Zeta, Fictional", _FSC_DATE, "AM",
        master_schedule=master_schedule, master_assist=master_assist,
        ambulatory_week=ambulatory_week, fsc_balances=fsc_balances,
    )
    assert not any(f.rule in ("not_eligible_rotation", "rotation_type_unconfirmed") for f in result.findings)


def test_inpatient_rotation_blocks():
    master_schedule, master_assist, ambulatory_week, fsc_balances = _fsc_base_inputs()
    master_schedule = [MasterScheduleWeek(resident_name="Zeta, Fictional", pgy=1, week_start=_FSC_WEEK_START, rotation="VA MICU")]
    result = check_fsc_reflection_day_request(
        "Zeta, Fictional", _FSC_DATE, "AM",
        master_schedule=master_schedule, master_assist=master_assist,
        ambulatory_week=ambulatory_week, fsc_balances=fsc_balances,
    )
    assert not result.is_clear
    assert any(f.rule == "not_eligible_rotation" and f.severity == "blocking" for f in result.findings)


def test_on_assist_list_blocks_regardless_of_duty_tag():
    """Being listed on the Master Assist List at all that week blocks — not
    just a JEOPARDY tag specifically."""
    master_schedule, _, ambulatory_week, fsc_balances = _fsc_base_inputs()
    master_assist = [
        MasterAssistDuty(resident_name="Zeta, Fictional", pgy_tier="PGY-1", week_start=_FSC_WEEK_START, duty="Pickett", extra=None),
    ]
    result = check_fsc_reflection_day_request(
        "Zeta, Fictional", _FSC_DATE, "AM",
        master_schedule=master_schedule, master_assist=master_assist,
        ambulatory_week=ambulatory_week, fsc_balances=fsc_balances,
    )
    assert not result.is_clear
    assert any(f.rule == "on_assist_list" and f.severity == "blocking" for f in result.findings)


def test_own_continuity_clinic_day_blocks():
    master_schedule, master_assist, _, fsc_balances = _fsc_base_inputs()
    ambulatory_week = [
        AmbulatoryWeekRow(resident_name="Zeta, Fictional", pgy=1, rotation="AMB Endo", day_parts={(_FSC_DATE, "AM"): "DOC"}),
    ]
    result = check_fsc_reflection_day_request(
        "Zeta, Fictional", _FSC_DATE, "AM",
        master_schedule=master_schedule, master_assist=master_assist,
        ambulatory_week=ambulatory_week, fsc_balances=fsc_balances,
    )
    assert not result.is_clear
    finding = next(f for f in result.findings if f.rule == "own_continuity_clinic")
    assert "AM" in finding.message


def test_own_continuity_clinic_day_does_not_block_a_different_half_day():
    """Requesting the PM half when only the AM half is their own CC day
    must not block."""
    master_schedule, master_assist, _, fsc_balances = _fsc_base_inputs()
    ambulatory_week = [
        AmbulatoryWeekRow(resident_name="Zeta, Fictional", pgy=1, rotation="AMB Endo", day_parts={(_FSC_DATE, "AM"): "DOC", (_FSC_DATE, "PM"): "AHD"}),
    ]
    result = check_fsc_reflection_day_request(
        "Zeta, Fictional", _FSC_DATE, "PM",
        master_schedule=master_schedule, master_assist=master_assist,
        ambulatory_week=ambulatory_week, fsc_balances=fsc_balances,
    )
    assert not any(f.rule == "own_continuity_clinic" for f in result.findings)


def test_insufficient_fsc_balance_is_a_warning_not_blocking():
    master_schedule, master_assist, ambulatory_week, _ = _fsc_base_inputs()
    fsc_balances = [
        FscBalance(resident_name="Zeta, Fictional", pgy=1, program="Categorical", base_fsc=4.0, fsc_available=4.0, fsc_used=4.0, fsc_left=0.0, phase="Appointment Time"),
    ]
    result = check_fsc_reflection_day_request(
        "Zeta, Fictional", _FSC_DATE, "FULL",
        master_schedule=master_schedule, master_assist=master_assist,
        ambulatory_week=ambulatory_week, fsc_balances=fsc_balances,
    )
    finding = next(f for f in result.findings if f.rule == "insufficient_fsc_balance")
    assert finding.severity == "warning"
    assert result.is_clear  # a warning alone doesn't block


def test_unrecognized_rotation_type_is_a_warning():
    master_schedule, master_assist, ambulatory_week, fsc_balances = _fsc_base_inputs()
    master_schedule = [MasterScheduleWeek(resident_name="Zeta, Fictional", pgy=1, week_start=_FSC_WEEK_START, rotation="Some Bespoke Elective")]
    result = check_fsc_reflection_day_request(
        "Zeta, Fictional", _FSC_DATE, "AM",
        master_schedule=master_schedule, master_assist=master_assist,
        ambulatory_week=ambulatory_week, fsc_balances=fsc_balances,
    )
    finding = next(f for f in result.findings if f.rule == "rotation_type_unconfirmed")
    assert finding.severity == "warning"
    assert result.is_clear


def test_full_day_costs_more_than_half_day():
    master_schedule, master_assist, ambulatory_week, _ = _fsc_base_inputs()
    fsc_balances = [
        FscBalance(resident_name="Zeta, Fictional", pgy=1, program="Categorical", base_fsc=4.0, fsc_available=4.0, fsc_used=3.5, fsc_left=0.5, phase="Appointment Time"),
    ]
    half_day_result = check_fsc_reflection_day_request(
        "Zeta, Fictional", _FSC_DATE, "AM",
        master_schedule=master_schedule, master_assist=master_assist,
        ambulatory_week=ambulatory_week, fsc_balances=fsc_balances,
    )
    full_day_result = check_fsc_reflection_day_request(
        "Zeta, Fictional", _FSC_DATE, "FULL",
        master_schedule=master_schedule, master_assist=master_assist,
        ambulatory_week=ambulatory_week, fsc_balances=fsc_balances,
    )
    assert not any(f.rule == "insufficient_fsc_balance" for f in half_day_result.findings)
    assert any(f.rule == "insufficient_fsc_balance" for f in full_day_result.findings)


# --- Tool 4: check_rotation_swap --------------------------------------------

_SWAP_WEEK_1 = dt.date(2026, 7, 6)
_SWAP_WEEK_2 = dt.date(2026, 7, 13)


def _rotation_swap_base_inputs():
    master_schedule = [
        MasterScheduleWeek(resident_name="Eta, Fictional", pgy=2, week_start=_SWAP_WEEK_1, rotation="VA GM"),
        MasterScheduleWeek(resident_name="Eta, Fictional", pgy=2, week_start=_SWAP_WEEK_2, rotation="VA GM"),
        MasterScheduleWeek(resident_name="Theta, Fictional", pgy=2, week_start=_SWAP_WEEK_1, rotation="AMB Endo"),
        MasterScheduleWeek(resident_name="Theta, Fictional", pgy=2, week_start=_SWAP_WEEK_2, rotation="AMB Endo"),
    ]
    master_assist: list[MasterAssistDuty] = []
    return master_schedule, master_assist


def test_clean_rotation_swap_has_no_blocking_findings():
    master_schedule, master_assist = _rotation_swap_base_inputs()
    result = check_rotation_swap(
        "Eta, Fictional", "Theta, Fictional", [_SWAP_WEEK_1, _SWAP_WEEK_2],
        master_schedule=master_schedule, master_assist=master_assist,
    )
    assert result.is_clear
    assert len(result.reminders) == 1


def test_rotation_swap_pgy_mismatch_blocks():
    master_schedule, master_assist = _rotation_swap_base_inputs()
    master_schedule = [
        MasterScheduleWeek(resident_name="Eta, Fictional", pgy=2, week_start=_SWAP_WEEK_1, rotation="VA GM"),
        MasterScheduleWeek(resident_name="Theta, Fictional", pgy=3, week_start=_SWAP_WEEK_1, rotation="AMB Endo"),
    ]
    result = check_rotation_swap(
        "Eta, Fictional", "Theta, Fictional", [_SWAP_WEEK_1],
        master_schedule=master_schedule, master_assist=master_assist,
    )
    assert not result.is_clear
    assert any(f.rule == "pgy_mismatch" and f.severity == "blocking" for f in result.findings)


def test_rotation_swap_blocks_when_resident_on_leave():
    master_schedule, master_assist = _rotation_swap_base_inputs()
    master_schedule = [
        MasterScheduleWeek(resident_name="Eta, Fictional", pgy=2, week_start=_SWAP_WEEK_1, rotation="VAC 1"),
        MasterScheduleWeek(resident_name="Theta, Fictional", pgy=2, week_start=_SWAP_WEEK_1, rotation="AMB Endo"),
    ]
    result = check_rotation_swap(
        "Eta, Fictional", "Theta, Fictional", [_SWAP_WEEK_1],
        master_schedule=master_schedule, master_assist=master_assist,
    )
    assert not result.is_clear
    finding = next(f for f in result.findings if f.rule == "not_an_active_rotation")
    assert finding.resident_name == "Eta, Fictional"


def test_rotation_swap_blocks_when_resident_on_assist_list():
    master_schedule, _ = _rotation_swap_base_inputs()
    master_assist = [
        MasterAssistDuty(resident_name="Theta, Fictional", pgy_tier="PGY-2", week_start=_SWAP_WEEK_1, duty="Pickett", extra=None),
    ]
    result = check_rotation_swap(
        "Eta, Fictional", "Theta, Fictional", [_SWAP_WEEK_1],
        master_schedule=master_schedule, master_assist=master_assist,
    )
    assert not result.is_clear
    finding = next(f for f in result.findings if f.rule == "on_assist_list")
    assert finding.resident_name == "Theta, Fictional"


def test_rotation_swap_unknown_rotation_is_a_warning():
    master_schedule, master_assist = _rotation_swap_base_inputs()
    result = check_rotation_swap(
        "Eta, Fictional", "Theta, Fictional", [_SWAP_WEEK_1, dt.date(2026, 8, 3)],
        master_schedule=master_schedule, master_assist=master_assist,
    )
    finding = next(f for f in result.findings if f.rule == "rotation_unknown")
    assert finding.severity == "warning"


# --- Tool 5: check_day_off_alignment ----------------------------------------

_TARGET_DATE = dt.date(2026, 7, 11)  # a Saturday


def test_currently_off_has_no_blocking_findings():
    inpatient_week_rows = [
        InpatientDayRow(team="GM1", resident_name="Iota, Fictional", day_parts={_TARGET_DATE: "OFF"}),
        InpatientDayRow(team="GM2", resident_name="Kappa, Fictional", day_parts={_TARGET_DATE: "On"}),
    ]
    result = check_day_off_alignment("Iota, Fictional", _TARGET_DATE, inpatient_week_rows=inpatient_week_rows)
    assert result.is_clear
    assert result.currently_off is True
    assert result.current_team == "GM1"
    assert result.findings == []


def test_resident_not_found_blocks():
    inpatient_week_rows = [
        InpatientDayRow(team="GM1", resident_name="Iota, Fictional", day_parts={_TARGET_DATE: "OFF"}),
    ]
    result = check_day_off_alignment("Nobody, Fictional", _TARGET_DATE, inpatient_week_rows=inpatient_week_rows)
    assert not result.is_clear
    assert any(f.rule == "resident_not_found" and f.severity == "blocking" for f in result.findings)


def test_finds_alternative_teams_with_day_off():
    inpatient_week_rows = [
        InpatientDayRow(team="GM1", resident_name="Iota, Fictional", day_parts={_TARGET_DATE: "On"}),
        InpatientDayRow(team="GM2", resident_name="Kappa, Fictional", day_parts={_TARGET_DATE: "OFF"}),
        InpatientDayRow(team="GM3", resident_name="Lambda, Fictional", day_parts={_TARGET_DATE: "OFF"}),
        InpatientDayRow(team="GM4", resident_name="Mu, Fictional", day_parts={_TARGET_DATE: "Post"}),
    ]
    result = check_day_off_alignment("Iota, Fictional", _TARGET_DATE, inpatient_week_rows=inpatient_week_rows)
    assert result.is_clear  # a warning alone doesn't block
    assert result.currently_off is False
    assert result.alternative_teams_with_day_off == ["GM2", "GM3"]
    assert any(f.rule == "alternative_teams_available" and f.severity == "warning" for f in result.findings)


def test_no_team_has_day_off_is_a_warning_not_blocking():
    inpatient_week_rows = [
        InpatientDayRow(team="GM1", resident_name="Iota, Fictional", day_parts={_TARGET_DATE: "On"}),
        InpatientDayRow(team="GM2", resident_name="Kappa, Fictional", day_parts={_TARGET_DATE: "Post"}),
    ]
    result = check_day_off_alignment("Iota, Fictional", _TARGET_DATE, inpatient_week_rows=inpatient_week_rows)
    assert result.is_clear
    assert result.alternative_teams_with_day_off == []
    finding = next(f for f in result.findings if f.rule == "no_team_has_day_off")
    assert finding.severity == "warning"


def test_alternative_teams_excludes_blank_team_labels():
    """Confirmed live: some real inpatient rows have a blank team column
    (e.g. a sub-row under a shared group label) — a blank "team" isn't an
    actionable reassignment suggestion for the chief."""
    inpatient_week_rows = [
        InpatientDayRow(team="GM1", resident_name="Iota, Fictional", day_parts={_TARGET_DATE: "On"}),
        InpatientDayRow(team="", resident_name="Nu, Fictional", day_parts={_TARGET_DATE: "OFF"}),
        InpatientDayRow(team="GM2", resident_name="Kappa, Fictional", day_parts={_TARGET_DATE: "OFF"}),
    ]
    result = check_day_off_alignment("Iota, Fictional", _TARGET_DATE, inpatient_week_rows=inpatient_week_rows)
    assert result.alternative_teams_with_day_off == ["GM2"]


def test_only_considers_the_target_date_not_other_weeks():
    """A row for the same resident from a DIFFERENT week (target_date not
    in its day_parts) must not be mistaken for their current assignment."""
    other_week_date = dt.date(2026, 7, 18)
    inpatient_week_rows = [
        InpatientDayRow(team="GM1", resident_name="Iota, Fictional", day_parts={other_week_date: "OFF"}),
        InpatientDayRow(team="GM2", resident_name="Iota, Fictional", day_parts={_TARGET_DATE: "On"}),
    ]
    result = check_day_off_alignment("Iota, Fictional", _TARGET_DATE, inpatient_week_rows=inpatient_week_rows)
    assert result.current_team == "GM2"
    assert result.currently_off is False
