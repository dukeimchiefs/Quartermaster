"""Tests for real_schedule/common.py's pure parsing helpers. All inputs are
fictional strings shaped like the real formats confirmed live (2026-07)
against Resident_Schedules/ — no real names or data.
"""

from __future__ import annotations

import datetime as dt

from real_schedule.common import (
    canonical_week_start,
    is_jeopardy_duty,
    is_jeopardy_label,
    is_non_committing_label,
    is_preceptor_cell,
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
