"""Shared parsing helpers for real_schedule/ readers.

These workbooks are human-maintained: merged cells, subtotal/spacer rows,
inconsistent free text, and section headers mixed into otherwise-tabular
data are all expected, not exceptional. Every helper here degrades to a
warning on unexpected input rather than raising — a single malformed row
must never take down a reader that's otherwise parsing hundreds of good
ones. Verified live (2026-07) against real weekly_ASSIST_List sheets: one
sampled week had a call-out-log row that didn't match the expected
Date/Rotation/Resident/Reason shape at all (a free-text note squeezed into
the date column) — this is the normal case to plan for, not an edge case.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

_PGY_RE = re.compile(r"(\d)")
_DUTY_CELL_RE = re.compile(r"^\s*(?P<name>[^(]+?)\s*\((?P<duty>[^)]+)\)\s*(?P<extra>.*)$")
_WEEK_LABEL_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\s*-\s*(\d{1,2})\.(\d{1,2})")
_BARE_NAME_PART_RE = re.compile(r"^[A-Za-z][A-Za-z'.\-]*(?:\s+[A-Za-z][A-Za-z'.\-]*){0,2}$")

_EXCEL_ERROR_STRINGS = frozenset({"#REF!", "#N/A", "#VALUE!", "#DIV/0!", "#NAME?", "#NULL!", "#NUM!"})
"""Confirmed live: at least one weekly_ASSIST_List sheet ("6.15-6.21", a
stale pre-season draft superseded by a "(lauren)" copy) has broken Excel
formula references that surface as these literal strings via openpyxl's
data_only mode, not as None — treated as blank, never as real name/duty
text."""


@dataclass(frozen=True)
class ParseWarning:
    sheet: str
    row: int
    reason: str


def parse_name_last_first(last: object, first: object) -> str | None:
    """Join separate Last/First cells into one canonical "Last, First" key.
    Returns None if either half is missing/blank — callers should warn and
    skip, not fabricate a partial name."""
    last_s = str(last).strip() if last is not None else ""
    first_s = str(first).strip() if first is not None else ""
    if not last_s or not first_s or last_s in _EXCEL_ERROR_STRINGS or first_s in _EXCEL_ERROR_STRINGS:
        return None
    return f"{last_s}, {first_s}"


def _looks_like_last_first_name(raw: str) -> bool:
    """True if `raw` has the shape of a real "Last, First" name (one comma,
    each side a short run of name-like word tokens) rather than free-text
    prose that happens to contain a comma. Confirmed live: a Master Assist
    List footnote row — "*Victor Ayeni on jeopardy Monday 8/31-Friday 9/4,
    no designated day off" — has exactly one comma too, so a bare
    comma-count check isn't enough; this also rejects any side with a digit
    or more than three words, which the footnote's date-laden left side
    trips on."""
    if raw.count(",") != 1:
        return False
    last, _, first = raw.partition(",")
    return bool(_BARE_NAME_PART_RE.match(last.strip())) and bool(_BARE_NAME_PART_RE.match(first.strip()))


def parse_duty_cell(text: object) -> tuple[str, str, str | None] | None:
    """Parse a Master Assist List free-text cell into (name, duty,
    extra_note). Handles three real shapes, confirmed live:
    - "Marks, Benjamin (JEOPARDY)" -> ("Marks, Benjamin", "JEOPARDY", None)
    - "Lefebvre, Maggie (JEOPARDY) Post call 7/6" -> (..., "JEOPARDY", "Post call 7/6")
    - A bare "Ma, Symon" with no parenthetical at all -> ("Ma, Symon", "", None).
      This is NOT a parse failure — plenty of real cells are just a name
      with no duty annotation (roughly an eighth of all cells, confirmed
      live) — it means "no specific duty noted," not "unparseable."
    Returns None only when there's no comma-separated "Last, First" shape
    to extract at all — genuinely unparseable, callers should warn. This
    also covers free-text footnote/annotation rows that happen to contain a
    comma (e.g. "*Victor Ayeni on jeopardy Monday 8/31-Friday 9/4, no
    designated day off", confirmed live) — see _looks_like_last_first_name.
    Some cells carry a SECOND, trailing parenthetical duty tag past the
    first, e.g. "Irvin, Jessica (Pickett) (jeopardy-I)" — confirmed live —
    meaning a resident's primary duty (Pickett) and jeopardy status can be
    layered in one cell. Use is_jeopardy_duty(duty, extra) rather than
    checking `duty` alone to catch this."""
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    match = _DUTY_CELL_RE.match(raw)
    if match is not None:
        name = match.group("name").strip()
        duty = match.group("duty").strip()
        extra = match.group("extra").strip() or None
        if name and duty:
            return name, duty, extra
    # No (duty) parenthetical at all -> treat the whole cell as a bare name,
    # but only if it actually looks like one (see _looks_like_last_first_name)
    # rather than free-text notes that happen to contain a comma. Footnote
    # markers appear on either end of a real name too (e.g. "Yalin, Elgin*",
    # confirmed live) — stripped before the shape check, same convention as
    # is_preceptor_cell's trailing */^ handling.
    if "(" not in raw:
        candidate = raw.strip("*^").strip()
        if _looks_like_last_first_name(candidate):
            return candidate, "", None
    return None


def is_jeopardy_duty(duty: str, extra: str | None) -> bool:
    """True if jeopardy is mentioned anywhere in a Master Assist List duty
    cell's parsed parts — either as the primary duty tag or a trailing
    annotation (e.g. "(Pickett) (jeopardy-I)", where Pickett is the primary
    continuity-clinic duty and jeopardy-I is layered on top)."""
    return "jeopardy" in duty.lower() or (extra is not None and "jeopardy" in extra.lower())


def normalize_pgy(cell: object) -> int | None:
    """Extract a PGY year as an int from values like "PGY1", "PGY-2",
    "PGY 3", "PGY-3 +" (treated as 3 — the "+" / "and above" qualifier on
    section labels like "PGY-3 +" is not distinguished from plain PGY-3;
    that distinction, if it ever matters, is a future refinement, not
    something to guess at now). Returns None — not a guess — for anything
    that doesn't contain a digit at all (e.g. "Resident out", a stray name
    that leaked into a Year-like column from an unrelated sub-table)."""
    if cell is None:
        return None
    match = _PGY_RE.search(str(cell))
    if match is None:
        return None
    return int(match.group(1))


JEOPARDY_LABELS = frozenset({"JEOPARDY", "JEOPARDY - I", "JEOPARDY - O"})
"""All three variants are treated as equivalent "on jeopardy" — confirmed
by the chief resident that the I/O distinction is arbitrary."""


def is_jeopardy_label(rotation: object) -> bool:
    """True for any rotation-cell value denoting jeopardy duty. Matches by
    prefix ("JEOPARDY...") rather than exact string equality against
    JEOPARDY_LABELS — confirmed live that master_MASTER_Schedule's actual
    cell text varies in spacing/punctuation around the I/O suffix (e.g.
    "JEOPARDY- I", no space before the dash, vs. weekly_ASSIST_List's own
    "JEOPARDY - I") in ways an exact match would wrongly treat as two
    different rotations — this was a real bug caught via the retroactive
    smoke test against an already-completed real swap, not a hypothetical."""
    if rotation is None:
        return False
    return str(rotation).strip().upper().replace(" ", "").startswith("JEOPARDY")


_NON_COMMITMENT_LABELS = frozenset({"OFF", "OFF*"})
_LEAVE_PREFIXES = ("VAC", "LOA")


def is_non_committing_label(rotation: object) -> bool:
    """True for a rotation-cell value that does NOT represent a real
    scheduling commitment that week — jeopardy/assist itself, an off week,
    or a leave/vacation code (VAC / VAC (flex) / VAC 1/2/3 / LOA). Anything
    else is treated as "this resident is committed elsewhere that week" for
    conflicting-commitment checks."""
    if rotation is None:
        return True
    text = str(rotation).strip().upper()
    if not text:
        return True
    if is_jeopardy_label(rotation):
        return True
    if text in _NON_COMMITMENT_LABELS:
        return True
    return any(text.startswith(prefix) for prefix in _LEAVE_PREFIXES)


def canonical_week_start(label: str, *, academic_year_start: int) -> dt.date | None:
    """Resolve a sheet-name-style week label (e.g. "B1. 7.6-7.12",
    "B1. 7.6-7.10", or a bare "6.15-6.21") to the Monday date it starts on.
    Different workbooks use different day-ranges for what's the same
    calendar week (weekly_ASSIST_List runs Mon-Sun, master_AMBULATORY_schedule
    runs Mon-Fri only) — this function is the one place that gets resolved,
    keyed on the Monday (the first M.D pair), never on string equality of
    the whole label, so the two workbooks' sheets for "the same week" always
    agree once passed through here.

    `academic_year_start` is the calendar year the academic year begins in
    (e.g. 2026 for the 2026-2027 year) — week labels carry no year of their
    own, so month >= 7 resolves to academic_year_start, month < 7 resolves
    to academic_year_start + 1 (matching this program's July-start academic
    calendar, confirmed via db/seed.py's own block-1-starts-July-1
    convention elsewhere in this codebase).

    Returns None (a warning for the caller to log, not an exception) if the
    label doesn't contain a recognizable M.D-M.D pattern at all.
    """
    match = _WEEK_LABEL_RE.search(label)
    if match is None:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    year = academic_year_start if month >= 7 else academic_year_start + 1
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


_PRECEPTOR_CELL_RE = re.compile(r"\(([^()]+)\)\s*[*^]*\s*$")


def is_preceptor_cell(text: object) -> tuple[str, str] | None:
    """Classify an ambulatory/available-clinics day-part cell. Returns
    (preceptor_name, clinic_site_code) for a subspecialty-clinic cell whose
    first line is the preceptor's name and whose LAST parenthesized group
    is the clinic site code — the common case is "Name\\n(SITE CODE)", but
    real cells also include a trailing footnote flag of "*" or "^" (e.g.
    "Rodger Liddle\\n(VA 1H)*", "Sophia Weinmann\\n(SD)^") and sometimes an
    extra descriptive line between the name and the site code (e.g.
    "Shelley McDonald\\nDuke POSH\\n(DS 1J)") — all confirmed live. Only a
    cell with this overall shape represents an actual preceptor
    relationship. Returns None for a bare CC-panel/admin placeholder (e.g.
    "Pickett(#)", "DOC", "AHD", "Post-Call", "Jeopardy"), a free-text note
    with no parenthetical at all (e.g. "Variable Schedule- Check Epic for
    availability"), or an empty cell — those are the resident's own
    continuity-clinic time or an unstructured note, not a specific
    preceptor's clinic, even though some CC-panel short codes (ACC, DOC)
    collide lexically with Location values used elsewhere for real
    preceptors. Classification is by shape (a newline, then eventually a
    trailing parenthetical), never by code value alone — get this wrong and
    Tool 2 either misses an affected resident or invents one, so this
    function is the single, unit-tested place that decision lives.
    """
    if text is None:
        return None
    raw = str(text)
    if "\n" not in raw:
        return None
    name_part, _, rest = raw.partition("\n")
    match = _PRECEPTOR_CELL_RE.search(rest)
    if match is None:
        return None
    name = name_part.strip()
    site_code = match.group(1).strip()
    if not name or not site_code:
        return None
    return name, site_code
