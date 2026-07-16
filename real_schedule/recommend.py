"""Propose ranked candidate (preceptor, location) reassignments for Check
Clinic Coverage, instead of requiring the chief resident to enter a
candidate preceptor and location by hand.

Restricted to the SAME specialty as the preceptor who called out (confirmed
requirement — a resident's subspecialty exposure shouldn't get swapped to an
unrelated field), then ranked by clinical fit (no blocking issues first) and
a "keep it local" locality heuristic. No distance or campus field exists
anywhere in Resident_Schedules/ (confirmed by search), so locality is
approximated by grouping the location/site-code string's leading token
(e.g. "VA 1H" -> "VA", "DS 1J" -> "DS") — a best-effort proxy, not a verified
geographic calculation, same documentation convention real_schedule/common.py
already uses for its own curated heuristics (e.g. _INPATIENT_TOKENS).

Pure function over already-loaded reader output, same posture as
real_schedule/checks.py — no file I/O here, never writes anything.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from real_schedule.available_clinics import ClinicSlot
from real_schedule.checks import CheckFinding, check_clinic_reassignment

MAX_RECOMMENDATIONS = 5

_NAME_TOKEN_RE = re.compile(r"[^,\s]+")

_WEEKDAY_LABELS = {0: "Mon", 1: "Tues", 2: "Wed", 3: "Thurs", 4: "Fri", 5: "Sat", 6: "Sun"}
"""available_day_parts is keyed on the ambulatory/available-clinics sheets'
own day-part column labels ("Mon AM", "Tues AM", ..., "Thurs AM", ...) — NOT
Python's date.strftime("%a") abbreviations, which disagree for Tuesday
("Tue" vs "Tues") and Thursday ("Thu" vs "Thurs")."""


def _same_preceptor(a: str, b: str) -> bool:
    """True if two preceptor-name strings denote the same person despite
    differing conventions between the two source workbooks: ambulatory
    schedule cells use "First Last" (real_schedule.common.is_preceptor_cell
    parses "Amit Sharma\\n(SITE)"), while the Available Clinics workbook's
    own Name column uses "Last, First" ("Sharma, Amit") — confirmed live,
    the same two-token name in opposite order. No canonical preceptor
    roster exists to resolve this properly (unlike residents, who have
    real_schedule.roster.RosterIndex) — matched here by tokenizing on
    comma/whitespace and comparing sets, same convention RosterIndex.
    canonicalize() uses for residents."""
    tokens_a = frozenset(t.lower() for t in _NAME_TOKEN_RE.findall(a))
    tokens_b = frozenset(t.lower() for t in _NAME_TOKEN_RE.findall(b))
    return bool(tokens_a) and tokens_a == tokens_b


@dataclass(frozen=True)
class RankedClinicCandidate:
    preceptor_name: str
    location: str | None
    specialty: str | None
    is_clear: bool
    findings: list[CheckFinding] = field(default_factory=list)
    same_site_group: bool = False
    rank: int = 0


def _site_group(location: str | None) -> str:
    """Best-effort locality proxy: the leading token of a location/site-code
    string ("VA 1H" -> "VA", "DS 1J" -> "DS", "Cary" -> "CARY"). Not a
    verified distance/campus lookup — none exists in the real data."""
    if not location:
        return ""
    return location.strip().split()[0].upper()


def recommend_clinic_coverage(
    called_out_preceptor: str,
    date_: dt.date,
    half_day: str,
    *,
    ambulatory_week,
    available_clinics: list[ClinicSlot],
    max_candidates: int = MAX_RECOMMENDATIONS,
) -> list[RankedClinicCandidate]:
    """Find and rank candidate (preceptor, location) reassignments for a
    preceptor who called out on `date_`/`half_day`. Restricted to the same
    specialty as the called-out preceptor (resolved from `available_clinics`
    — if that preceptor has no entry there, their specialty is unknown and
    this returns an empty list rather than guessing), available that
    weekday/half-day, excluding the called-out preceptor itself.

    Ranked: no blocking findings first, then same "site group" as the
    called-out preceptor's own location, then fewest warning-level
    findings, then alphabetically by preceptor name. Returns up to
    `max_candidates`, each already validated via check_clinic_reassignment
    so the chief sees the same findings a manual check would have produced.
    """
    called_out_slot = next(
        (slot for slot in available_clinics if _same_preceptor(slot.preceptor_name, called_out_preceptor)), None
    )
    if called_out_slot is None or not called_out_slot.specialty:
        return []

    weekday_key = _WEEKDAY_LABELS[date_.weekday()]
    called_out_group = _site_group(called_out_slot.location)

    unranked: list[RankedClinicCandidate] = []
    seen: set[tuple[str, str | None]] = set()
    for slot in available_clinics:
        if _same_preceptor(slot.preceptor_name, called_out_preceptor) or slot.specialty != called_out_slot.specialty:
            continue
        if (weekday_key, half_day) not in slot.available_day_parts:
            continue
        location = slot.site_codes_by_day_part.get((weekday_key, half_day)) or slot.location
        key = (slot.preceptor_name, location)
        if key in seen:
            continue
        seen.add(key)

        result = check_clinic_reassignment(
            called_out_preceptor,
            date_,
            half_day,
            slot.preceptor_name,
            location,
            ambulatory_week=ambulatory_week,
            available_clinics=available_clinics,
        )
        unranked.append(
            RankedClinicCandidate(
                preceptor_name=slot.preceptor_name,
                location=location,
                specialty=slot.specialty,
                is_clear=result.is_clear,
                findings=result.findings,
                same_site_group=called_out_group != "" and _site_group(location) == called_out_group,
            )
        )

    unranked.sort(
        key=lambda c: (
            not c.is_clear,
            not c.same_site_group,
            sum(1 for f in c.findings if f.severity == "warning"),
            c.preceptor_name,
        )
    )
    return [
        RankedClinicCandidate(
            preceptor_name=c.preceptor_name,
            location=c.location,
            specialty=c.specialty,
            is_clear=c.is_clear,
            findings=c.findings,
            same_site_group=c.same_site_group,
            rank=i + 1,
        )
        for i, c in enumerate(unranked[:max_candidates])
    ]
