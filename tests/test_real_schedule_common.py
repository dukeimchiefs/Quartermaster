"""Tests for real_schedule/common.py's pure parsing helpers. All inputs are
fictional strings shaped like the real formats confirmed live (2026-07)
against Resident_Schedules/ — no real names or data.
"""

from __future__ import annotations

import datetime as dt

from real_schedule.common import (
    canonical_week_start,
    is_continuity_clinic_cell,
    is_day_off_cell,
    is_inpatient_rotation,
    is_jeopardy_duty,
    is_jeopardy_label,
    is_non_committing_label,
    is_preceptor_cell,
    is_recognized_ambulatory_rotation,
    normalize_pgy,
    parse_duty_cell,
    parse_name_last_first,
)


def test_parse_name_last_first_joins_correctly():
    assert parse_name_last_first("Chen", "Alice") == "Chen, Alice"


def test_parse_name_last_first_none_on_missing_half():
    assert parse_name_last_first("Chen", None) is None
    assert parse_name_last_first(None, "Alice") is None
    assert parse_name_last_first("", "Alice") is None


def test_parse_name_last_first_none_on_excel_error_string():
    """Confirmed live: a stale weekly_ASSIST_List sheet had a broken formula
    reference surfacing as the literal string "#REF!" (via openpyxl's
    data_only mode) in place of a real first name."""
    assert parse_name_last_first("Adedipe", "#REF!") is None


def test_parse_duty_cell_simple_shape():
    assert parse_duty_cell("Chen, Alice (JEOPARDY)") == ("Chen, Alice", "JEOPARDY", None)


def test_parse_duty_cell_with_trailing_note():
    assert parse_duty_cell("Chen, Alice (JEOPARDY) Post call 7/6") == ("Chen, Alice", "JEOPARDY", "Post call 7/6")


def test_parse_duty_cell_bare_name_no_parenthetical():
    """A name with no duty tag at all is a legitimate shape, not a parse
    failure — confirmed live (~1/8 of real Master Assist List cells)."""
    assert parse_duty_cell("Chen, Alice") == ("Chen, Alice", "", None)


def test_parse_duty_cell_none_for_unparseable():
    assert parse_duty_cell("not a name at all") is None
    assert parse_duty_cell(None) is None
    assert parse_duty_cell("") is None


def test_parse_duty_cell_rejects_footnote_note_with_a_comma():
    """Confirmed live: a Master Assist List footnote row can contain
    exactly one comma too, so a bare comma-count check would wrongly accept
    it as a "Last, First" name."""
    assert parse_duty_cell("*Victor Ayeni on jeopardy Monday 8/31-Friday 9/4, no designated day off") is None


def test_parse_duty_cell_bare_name_with_footnote_marker():
    """Confirmed live: footnote markers ("*"/"^") can appear on either end
    of an otherwise-real bare name, e.g. "Yalin, Elgin*"."""
    assert parse_duty_cell("Yalin, Elgin*") == ("Yalin, Elgin", "", None)
    assert parse_duty_cell("*Chen, Alice") == ("Chen, Alice", "", None)


def test_is_jeopardy_duty_catches_primary_tag():
    assert is_jeopardy_duty("JEOPARDY", None) is True


def test_is_jeopardy_duty_catches_trailing_annotation():
    """Confirmed live: jeopardy can be a trailing tag on top of a
    different primary duty, e.g. "Chen, Alice (PRIME) (jeopardy-I)"."""
    assert is_jeopardy_duty("PRIME", "(jeopardy-I)") is True


def test_is_jeopardy_duty_false_when_absent():
    assert is_jeopardy_duty("PRIME", None) is False
    assert is_jeopardy_duty("DOC", "some other note") is False


def test_normalize_pgy_extracts_digit():
    assert normalize_pgy("PGY1") == 1
    assert normalize_pgy("PGY-2") == 2
    assert normalize_pgy("PGY 3") == 3
    assert normalize_pgy("PGY-3 +") == 3


def test_normalize_pgy_none_for_non_pgy_text():
    """Confirmed live: a Year-like column can contain stray non-PGY
    content leaked from an adjacent sub-table (e.g. "Resident out")."""
    assert normalize_pgy("Resident out") is None
    assert normalize_pgy(None) is None


def test_canonical_week_start_resolves_monday():
    assert canonical_week_start("B1. 7.6-7.12", academic_year_start=2026) == dt.date(2026, 7, 6)


def test_canonical_week_start_agrees_across_different_day_ranges():
    """weekly_ASSIST_List uses Mon-Sun labels, master_AMBULATORY_schedule
    uses Mon-Fri labels for the same calendar week — both must resolve to
    the same Monday."""
    mon_sun = canonical_week_start("B1. 7.6-7.12", academic_year_start=2026)
    mon_fri = canonical_week_start("B1. 7.6-7.10", academic_year_start=2026)
    assert mon_sun == mon_fri == dt.date(2026, 7, 6)


def test_canonical_week_start_rolls_into_next_calendar_year():
    """A January week label belongs to the calendar year AFTER
    academic_year_start, since the academic year begins in July."""
    assert canonical_week_start("B5. 1.4-1.10", academic_year_start=2026) == dt.date(2027, 1, 4)


def test_canonical_week_start_none_for_unrecognizable_label():
    assert canonical_week_start("Template", academic_year_start=2026) is None


def test_is_preceptor_cell_simple_shape():
    assert is_preceptor_cell("Jane Doe\n(SD)") == ("Jane Doe", "SD")


def test_is_preceptor_cell_with_footnote_flags():
    assert is_preceptor_cell("Jane Doe\n(SD)*") == ("Jane Doe", "SD")
    assert is_preceptor_cell("Jane Doe\n(SD)^") == ("Jane Doe", "SD")
    assert is_preceptor_cell("Jane Doe\n(SD)*^") == ("Jane Doe", "SD")


def test_is_preceptor_cell_with_extra_descriptive_line():
    """Confirmed live: some cells have a middle descriptive line between
    the name and the site code, e.g. "Jane Doe\\nSome Clinic\\n(SD)"."""
    assert is_preceptor_cell("Jane Doe\nSome Clinic\n(SD)") == ("Jane Doe", "SD")


def test_is_preceptor_cell_false_for_cc_panel_placeholder():
    """No newline at all -> not a preceptor cell, regardless of parens."""
    assert is_preceptor_cell("Pickett(#)") is None
    assert is_preceptor_cell("DOC") is None
    assert is_preceptor_cell("Post-Call") is None


def test_is_preceptor_cell_false_for_freetext_note():
    assert is_preceptor_cell("Variable schedule, check Epic") is None


def test_is_preceptor_cell_none_for_empty():
    assert is_preceptor_cell(None) is None


def test_is_jeopardy_label_matches_all_real_spacing_variants():
    """Confirmed live: real cell text varies in spacing/punctuation around
    the I/O suffix (e.g. master_MASTER_Schedule's "JEOPARDY- I" vs.
    weekly_ASSIST_List's "JEOPARDY - I") — this was a real bug, caught via
    a retroactive check against an already-completed real swap, where an
    exact-match comparison wrongly treated a resident's own new jeopardy
    week as a conflicting ordinary-rotation commitment."""
    assert is_jeopardy_label("JEOPARDY") is True
    assert is_jeopardy_label("JEOPARDY - I") is True
    assert is_jeopardy_label("JEOPARDY- I") is True
    assert is_jeopardy_label("JEOPARDY-I") is True
    assert is_jeopardy_label("jeopardy - o") is True
    assert is_jeopardy_label("VA GM") is False
    assert is_jeopardy_label(None) is False


def test_is_non_committing_label_true_for_jeopardy_off_and_leave():
    assert is_non_committing_label("JEOPARDY") is True
    assert is_non_committing_label("OFF") is True
    assert is_non_committing_label("VAC 1") is True
    assert is_non_committing_label("VAC (flex)") is True
    assert is_non_committing_label("LOA") is True
    assert is_non_committing_label(None) is True


def test_is_non_committing_label_false_for_ordinary_rotation():
    assert is_non_committing_label("VA GM") is False
    assert is_non_committing_label("AMB Endo") is False


def test_is_continuity_clinic_cell_matches_doc_prime_pickett_and_typo_variants():
    """Confirmed live: this family has heavy typo/punctuation/suffix
    variance in real ambulatory day-part cells."""
    assert is_continuity_clinic_cell("DOC") is True
    assert is_continuity_clinic_cell("DOC(#)") is True
    assert is_continuity_clinic_cell("DOC(%)") is True
    assert is_continuity_clinic_cell("D.O.C. Admin Time") is True
    assert is_continuity_clinic_cell("D.O.C. Endo.crine*") is True
    assert is_continuity_clinic_cell("PRIME") is True
    assert is_continuity_clinic_cell("PRIME(#1)") is True
    assert is_continuity_clinic_cell("P.RIME Orientation") is True
    assert is_continuity_clinic_cell("Pickett") is True
    assert is_continuity_clinic_cell("Pickett(%)") is True
    assert is_continuity_clinic_cell("P.icket Admin") is True


def test_is_continuity_clinic_cell_false_for_unrelated_placeholders():
    assert is_continuity_clinic_cell("AAU") is False
    assert is_continuity_clinic_cell("Post-Call") is False
    assert is_continuity_clinic_cell("VA Renal") is False
    assert is_continuity_clinic_cell("Jeopardy") is False
    assert is_continuity_clinic_cell("AHD") is False
    assert is_continuity_clinic_cell(None) is False


def test_is_continuity_clinic_cell_false_for_unrelated_cc_code():
    """"CC Modules"/"CC Shirey" are a different, unrelated use of "CC" in
    this workbook, not one of the three named panel types."""
    assert is_continuity_clinic_cell("CC Modules") is False


def test_is_continuity_clinic_cell_false_for_real_preceptor_cell():
    """A real subspecialty-preceptor relationship is never also the
    resident's own CC-panel time."""
    assert is_continuity_clinic_cell("Jane Doe\n(SD)") is False


def test_is_recognized_ambulatory_rotation_matches_amb_and_cs_prefixes():
    assert is_recognized_ambulatory_rotation("AMB Endo") is True
    assert is_recognized_ambulatory_rotation("MP AMB") is True
    assert is_recognized_ambulatory_rotation("POCUS/MP Amb") is True
    assert is_recognized_ambulatory_rotation("CS Endo") is True
    assert is_recognized_ambulatory_rotation("CS GM Proc") is True  # "CS" wins over the "GM" token
    assert is_recognized_ambulatory_rotation("SDE - AMB Cards") is True
    assert is_recognized_ambulatory_rotation("SDE - CS GI") is True
    assert is_recognized_ambulatory_rotation("BHIP") is True
    assert is_recognized_ambulatory_rotation("Sport Med") is True


def test_is_recognized_ambulatory_rotation_false_for_inpatient_and_unknown():
    assert is_recognized_ambulatory_rotation("VA MICU") is False
    assert is_recognized_ambulatory_rotation("Duke GM") is False
    assert is_recognized_ambulatory_rotation("Some Bespoke Elective") is False
    assert is_recognized_ambulatory_rotation(None) is False


def test_is_inpatient_rotation_matches_icu_nightfloat_and_gm_services():
    assert is_inpatient_rotation("VA MICU") is True
    assert is_inpatient_rotation("Duke GM") is True
    assert is_inpatient_rotation("GM12") is True
    assert is_inpatient_rotation("NF1") is True
    assert is_inpatient_rotation("NF-GP") is True
    assert is_inpatient_rotation("9100") is True
    assert is_inpatient_rotation("9100 NF") is True
    assert is_inpatient_rotation("DRH ACR") is True
    assert is_inpatient_rotation("Hospitalist") is True


def test_is_inpatient_rotation_false_when_ambulatory_prefix_should_win():
    """"CS GM Proc"/"AMB Med-Psych" contain a token ("GM"/"ED"-via-"MED")
    that would otherwise look inpatient-ish — callers must check
    is_recognized_ambulatory_rotation() first; this only documents that
    is_inpatient_rotation() itself doesn't false-positive on "MED"."""
    assert is_inpatient_rotation("AMB Med-Psych") is False


def test_is_inpatient_rotation_false_for_unknown():
    assert is_inpatient_rotation("Global Health") is False
    assert is_inpatient_rotation(None) is False


def test_is_day_off_cell_matches_off_and_annotated_variants():
    assert is_day_off_cell("OFF") is True
    assert is_day_off_cell("OFF ") is True
    assert is_day_off_cell("OFF (holiday)") is True
    assert is_day_off_cell("off") is True


def test_is_day_off_cell_false_for_unrelated_shift_codes():
    assert is_day_off_cell("On") is False
    assert is_day_off_cell("Pre") is False
    assert is_day_off_cell("NIGHT A") is False
    assert is_day_off_cell(None) is False


def test_is_day_off_cell_false_for_annotation_mentioning_off_elsewhere():
    """Confirmed live: "Pre (intern off)" is a real, distinct shift-type
    value (this resident's own shift is "Pre"; the parenthetical just
    notes the INTERN is off) — a substring "off" check would wrongly treat
    this resident as off too."""
    assert is_day_off_cell("Pre (intern off)") is False
    assert is_day_off_cell("POST/OFF") is False
