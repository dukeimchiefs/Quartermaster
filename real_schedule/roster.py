"""Reader for the internal roster, Resident_Schedules/duke_residency_2026-
2027.csv — the authoritative source for resident names, per the chief
resident's direction: every other workbook's own name formatting is
untrusted; this CSV wins.

Verified live (2026-07): 192 residents, columns `Name, Phone, Email,
Organization, Program, StartYear`. Confirmed real, live data-quality
problem this canonicalization step exists to fix: master_MASTER_Schedule's
own Name column has 45 of 237 (~19%) residents entered as "First Last"
instead of "Last, First" — enough to silently break cross-file joins in
checks.py, which match records by exact resident_name string equality.

The wrinkle: this CSV itself stores names as "First Last", not "Last,
First" — it needs reformatting too, not just lookup. 187 of 192 names are
a clean 2-token shape. The other 5 are 3+ token names where splitting is
genuinely ambiguous (confirmed live: "Mohummad Hassan Raza Raja" is
actually referred to elsewhere in the real data as "Raza Raja, Hassan" —
the middle token becomes the effective first name, dropping "Mohummad"
entirely; "Annie Kleynerman Roels" is referred to elsewhere as just
"Kleynerman, Annie", dropping the trailing token). No positional rule gets
every one of these right, and the correct per-person mapping can't be
hardcoded into git-tracked source (that would mean committing a real-name
lookup table, which this project's PII policy forbids). load_roster() uses
a documented, disclosed best-effort convention (last token = surname) for
these rare cases and emits a ParseWarning for every one, so it's visible,
not silently guessed. RosterIndex.canonicalize() separately tolerates this
ambiguity for MATCHING purposes (see its docstring) even where the display
string doesn't perfectly reproduce every token.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass

from real_schedule.common import ParseWarning

_NAME_TOKEN_RE = re.compile(r"[^,\s]+")


@dataclass(frozen=True)
class RosterEntry:
    canonical_name: str  # "Last, First"
    first: str
    last: str


def _tokens(name: str) -> frozenset[str]:
    return frozenset(t.lower() for t in _NAME_TOKEN_RE.findall(name))


def load_roster(path: str) -> tuple[list[RosterEntry], list[ParseWarning]]:
    warnings: list[ParseWarning] = []
    records: list[RosterEntry] = []

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row_number, row in enumerate(reader, start=2):  # header is row 1
            raw_name = (row.get("Name") or "").strip()
            if not raw_name:
                continue
            tokens = raw_name.split()
            if len(tokens) < 2:
                warnings.append(
                    ParseWarning(sheet="duke_residency_2026-2027.csv", row=row_number, reason=f"name {raw_name!r} has fewer than two tokens — can't split into first/last")
                )
                continue
            if len(tokens) == 2:
                first, last = tokens
            else:
                # 3+ tokens: genuinely ambiguous where first ends and last
                # begins (see module docstring) — last token = surname is a
                # documented default, not a verified-correct split for every
                # real case, so this is always flagged.
                first, last = " ".join(tokens[:-1]), tokens[-1]
                warnings.append(
                    ParseWarning(
                        sheet="duke_residency_2026-2027.csv",
                        row=row_number,
                        reason=f"name {raw_name!r} has more than two tokens — guessed split (last token as surname); verify manually",
                    )
                )
            records.append(RosterEntry(canonical_name=f"{last}, {first}", first=first, last=last))

    return records, warnings


class RosterIndex:
    def __init__(self, entries: list[RosterEntry]):
        self._entries = [(e, _tokens(e.canonical_name)) for e in entries]

    def canonicalize(self, name: str) -> tuple[str, bool]:
        """Returns (name_to_use, matched). Tokenizes `name` (split on comma/
        whitespace, case-insensitive) and matches against the roster by
        SUBSET comparison in either direction, not exact set equality — a
        query with fewer tokens than the roster entry (a dropped middle
        name) or more tokens than the roster entry both still match, which
        is what lets the "Kleynerman"/"Raza Raja" cases (see module
        docstring) match correctly even though the canonical display string
        doesn't literally reproduce every token from every source. Exactly
        one matching entry -> (canonical_name, True). Zero matches, or more
        than one (ambiguous) -> (name unchanged, False) — callers should
        treat this as informational, never fabricate a name."""
        if not name:
            return name, False
        query_tokens = _tokens(name)
        if not query_tokens:
            return name, False
        matches = [entry for entry, tokens in self._entries if query_tokens <= tokens or tokens <= query_tokens]
        if len(matches) == 1:
            return matches[0].canonical_name, True
        return name, False
