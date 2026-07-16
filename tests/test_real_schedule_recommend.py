"""Tests for real_schedule/recommend.py's recommend_clinic_coverage — small,
hand-built in-memory fixtures (fictional names), same pattern as
tests/test_real_schedule_checks.py.
"""

from __future__ import annotations

import datetime as dt

from real_schedule.ambulatory import AmbulatoryWeekRow
from real_schedule.available_clinics import ClinicSlot
from real_schedule.recommend import _same_preceptor, _site_group, recommend_clinic_coverage

_DATE = dt.date(2026, 7, 6)  # a Monday
_THURSDAY = dt.date(2026, 7, 9)


def test_recommend_restricts_to_same_specialty():
    ambulatory_week = [
        AmbulatoryWeekRow(resident_name="Delta, Fictional", pgy=1, rotation="AMB ID", day_parts={(_DATE, "AM"): "Dr. Out\n(SD)"}),
    ]
    available_clinics = [
        ClinicSlot(preceptor_name="Dr. Out", specialty="ID", location="SD", tier="Intern Blocks", available_day_parts={("Mon", "AM")}),
        ClinicSlot(
            preceptor_name="Dr. SameField", specialty="ID", location="SD", tier="Intern Blocks",
            available_day_parts={("Mon", "AM")}, site_codes_by_day_part={("Mon", "AM"): "SD"},
        ),
        ClinicSlot(
            preceptor_name="Dr. OtherField", specialty="Rheum", location="SD", tier="Intern Blocks",
            available_day_parts={("Mon", "AM")}, site_codes_by_day_part={("Mon", "AM"): "SD"},
        ),
    ]
    candidates = recommend_clinic_coverage("Dr. Out", _DATE, "AM", ambulatory_week=ambulatory_week, available_clinics=available_clinics)
    assert [c.preceptor_name for c in candidates] == ["Dr. SameField"]


def test_recommend_excludes_unavailable_and_called_out_preceptor():
    ambulatory_week = [
        AmbulatoryWeekRow(resident_name="Delta, Fictional", pgy=1, rotation="AMB ID", day_parts={(_DATE, "AM"): "Dr. Out\n(SD)"}),
    ]
    available_clinics = [
        ClinicSlot(preceptor_name="Dr. Out", specialty="ID", location="SD", tier="Intern Blocks", available_day_parts={("Mon", "AM")}),
        ClinicSlot(preceptor_name="Dr. WrongHalfDay", specialty="ID", location="SD", tier="Intern Blocks", available_day_parts={("Mon", "PM")}),
    ]
    candidates = recommend_clinic_coverage("Dr. Out", _DATE, "AM", ambulatory_week=ambulatory_week, available_clinics=available_clinics)
    assert candidates == []


def test_recommend_returns_empty_when_called_out_specialty_unknown():
    candidates = recommend_clinic_coverage("Nobody", _DATE, "AM", ambulatory_week=[], available_clinics=[])
    assert candidates == []


def test_recommend_ranks_same_site_group_before_other_and_clear_before_blocked():
    ambulatory_week = [
        AmbulatoryWeekRow(resident_name="Delta, Fictional", pgy=1, rotation="AMB ID", day_parts={(_DATE, "AM"): "Dr. Out\n(VA 1H)"}),
    ]
    available_clinics = [
        ClinicSlot(preceptor_name="Dr. Out", specialty="ID", location="VA 1H", tier="Intern Blocks", available_day_parts={("Mon", "AM")}),
        # Different site group (SD), but otherwise clean.
        ClinicSlot(
            preceptor_name="Dr. FarAway", specialty="ID", location="SD", tier="Intern Blocks",
            available_day_parts={("Mon", "AM")}, site_codes_by_day_part={("Mon", "AM"): "SD"},
        ),
        # Same site group (VA) as the called-out preceptor.
        ClinicSlot(
            preceptor_name="Dr. SameSite", specialty="ID", location="VA 2B", tier="Intern Blocks",
            available_day_parts={("Mon", "AM")}, site_codes_by_day_part={("Mon", "AM"): "VA 2B"},
        ),
    ]
    candidates = recommend_clinic_coverage("Dr. Out", _DATE, "AM", ambulatory_week=ambulatory_week, available_clinics=available_clinics)
    assert [c.preceptor_name for c in candidates] == ["Dr. SameSite", "Dr. FarAway"]
    assert candidates[0].same_site_group is True
    assert candidates[0].rank == 1
    assert candidates[1].same_site_group is False
    assert candidates[1].rank == 2


def test_recommend_caps_at_max_candidates():
    ambulatory_week = [
        AmbulatoryWeekRow(resident_name="Delta, Fictional", pgy=1, rotation="AMB ID", day_parts={(_DATE, "AM"): "Dr. Out\n(SD)"}),
    ]
    available_clinics = [ClinicSlot(preceptor_name="Dr. Out", specialty="ID", location="SD", tier="Intern Blocks", available_day_parts={("Mon", "AM")})]
    available_clinics += [
        ClinicSlot(
            preceptor_name=f"Dr. Candidate{i}", specialty="ID", location="SD", tier="Intern Blocks",
            available_day_parts={("Mon", "AM")}, site_codes_by_day_part={("Mon", "AM"): "SD"},
        )
        for i in range(8)
    ]
    candidates = recommend_clinic_coverage(
        "Dr. Out", _DATE, "AM", ambulatory_week=ambulatory_week, available_clinics=available_clinics, max_candidates=5
    )
    assert len(candidates) == 5
    assert [c.rank for c in candidates] == [1, 2, 3, 4, 5]


def test_site_group_takes_leading_token():
    assert _site_group("VA 1H") == "VA"
    assert _site_group("DS 1J") == "DS"
    assert _site_group("Cary") == "CARY"
    assert _site_group(None) == ""
    assert _site_group("") == ""


def test_recommend_matches_called_out_preceptor_despite_name_format_mismatch():
    """Confirmed live: the ambulatory schedule's own cells use "First Last"
    (real_schedule.common.is_preceptor_cell), while the Available Clinics
    workbook's Name column uses "Last, First" — the page passes whichever
    format ambulatory_week produced as called_out_preceptor, so resolving
    that preceptor's own specialty against available_clinics must not
    require an exact string match."""
    ambulatory_week = [
        AmbulatoryWeekRow(resident_name="Delta, Fictional", pgy=1, rotation="AMB ID", day_parts={(_DATE, "AM"): "Amit Sharma\n(DRaH ID Clinic)"}),
    ]
    available_clinics = [
        ClinicSlot(preceptor_name="Sharma, Amit", specialty="ID", location="DRaH ID Clinic", tier="Intern Blocks", available_day_parts={("Mon", "AM")}),
        ClinicSlot(
            preceptor_name="Turner, Nicholas", specialty="ID", location="DS 1K", tier="Intern Blocks",
            available_day_parts={("Mon", "AM")}, site_codes_by_day_part={("Mon", "AM"): "DS 1K"},
        ),
    ]
    candidates = recommend_clinic_coverage(
        "Amit Sharma", _DATE, "AM", ambulatory_week=ambulatory_week, available_clinics=available_clinics
    )
    assert [c.preceptor_name for c in candidates] == ["Turner, Nicholas"]


def test_recommend_resolves_tuesday_and_thursday_day_parts():
    """available_day_parts is keyed on the sheets' own "Tues"/"Thurs" column
    labels, not Python's strftime("%a") ("Tue"/"Thu") — a Thursday date must
    still resolve against a "Thurs" availability entry."""
    ambulatory_week = [
        AmbulatoryWeekRow(resident_name="Delta, Fictional", pgy=1, rotation="AMB ID", day_parts={(_THURSDAY, "AM"): "Dr. Out\n(SD)"}),
    ]
    available_clinics = [
        ClinicSlot(preceptor_name="Dr. Out", specialty="ID", location="SD", tier="Intern Blocks", available_day_parts={("Thurs", "AM")}),
        ClinicSlot(
            preceptor_name="Dr. Available", specialty="ID", location="SD", tier="Intern Blocks",
            available_day_parts={("Thurs", "AM")}, site_codes_by_day_part={("Thurs", "AM"): "SD"},
        ),
    ]
    candidates = recommend_clinic_coverage(
        "Dr. Out", _THURSDAY, "AM", ambulatory_week=ambulatory_week, available_clinics=available_clinics
    )
    assert [c.preceptor_name for c in candidates] == ["Dr. Available"]


def test_same_preceptor_matches_across_name_order():
    assert _same_preceptor("Amit Sharma", "Sharma, Amit")
    assert _same_preceptor("Dr. Out", "Dr. Out")
    assert not _same_preceptor("Amit Sharma", "Nicholas Turner")
    assert not _same_preceptor("", "")
