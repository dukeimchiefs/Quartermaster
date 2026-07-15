"""Tests for real_schedule/roster.py — the internal-roster reader and
canonicalizer used to force consistent "Last, First" resident names across
every real_schedule/ tool, per the chief resident's direction. Fictional
names only, shaped like the real ambiguity confirmed live (2026-07):
master_MASTER_Schedule has ~19% of residents entered as "First Last"
instead of "Last, First", and the roster CSV itself stores "First Last"
with a handful of genuinely ambiguous 3+-token names.
"""

from __future__ import annotations

import csv

from real_schedule.roster import RosterIndex, load_roster


def _write_roster_csv(tmp_path, rows: list[dict]):
    path = tmp_path / "duke_residency.csv"
    fieldnames = ["Name", "Phone", "Email", "Organization", "Program", "StartYear"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return str(path)


def test_load_roster_two_token_names(tmp_path):
    path = _write_roster_csv(
        tmp_path,
        [
            {"Name": "Alice Chen", "Phone": "", "Email": "", "Organization": "", "Program": "Categorical", "StartYear": "2026"},
            {"Name": "Ben Diaz", "Phone": "", "Email": "", "Organization": "", "Program": "Categorical", "StartYear": "2025"},
        ],
    )
    entries, warnings = load_roster(path)
    assert warnings == []
    assert len(entries) == 2
    assert entries[0].canonical_name == "Chen, Alice"
    assert entries[0].first == "Alice"
    assert entries[0].last == "Chen"


def test_load_roster_multi_token_name_falls_back_and_warns(tmp_path):
    """A fictional 3-token name shaped like the real ambiguous cases (e.g.
    "Mohummad Hassan Raza Raja") — last token = surname is a documented
    default, always flagged with a warning, never a silent guess."""
    path = _write_roster_csv(
        tmp_path,
        [{"Name": "Middle Family Surname", "Phone": "", "Email": "", "Organization": "", "Program": "Categorical", "StartYear": "2026"}],
    )
    entries, warnings = load_roster(path)
    assert len(entries) == 1
    assert entries[0].canonical_name == "Surname, Middle Family"
    assert len(warnings) == 1
    assert "more than two tokens" in warnings[0].reason


def test_load_roster_skips_blank_name_rows(tmp_path):
    path = _write_roster_csv(
        tmp_path,
        [
            {"Name": "Alice Chen", "Phone": "", "Email": "", "Organization": "", "Program": "Categorical", "StartYear": "2026"},
            {"Name": "", "Phone": "", "Email": "", "Organization": "", "Program": "", "StartYear": ""},
        ],
    )
    entries, warnings = load_roster(path)
    assert len(entries) == 1


def test_canonicalize_exact_match():
    index = RosterIndex([_entry("Chen, Alice", "Alice", "Chen")])
    name, matched = index.canonicalize("Chen, Alice")
    assert matched is True
    assert name == "Chen, Alice"


def test_canonicalize_matches_across_format_difference():
    """The core fix: a "First Last"-shaped input (like master_schedule's
    real ~19% formatting bug) must still resolve to the roster's canonical
    "Last, First" form."""
    index = RosterIndex([_entry("Chen, Alice", "Alice", "Chen")])
    name, matched = index.canonicalize("Alice Chen")
    assert matched is True
    assert name == "Chen, Alice"


def test_canonicalize_matches_when_query_drops_a_token():
    """Confirmed live: some real files refer to a resident by fewer tokens
    than the roster's full name (e.g. dropping a middle name) — subset
    matching in either direction handles this."""
    index = RosterIndex([_entry("Roels, Annie Kleynerman", "Annie Kleynerman", "Roels")])
    name, matched = index.canonicalize("Kleynerman, Annie")
    assert matched is True
    assert name == "Roels, Annie Kleynerman"


def test_canonicalize_matches_when_query_has_extra_token():
    index = RosterIndex([_entry("Chen, Alice", "Alice", "Chen")])
    name, matched = index.canonicalize("Alice Middle Chen")
    assert matched is True
    assert name == "Chen, Alice"


def test_canonicalize_no_match_returns_original_unchanged():
    index = RosterIndex([_entry("Chen, Alice", "Alice", "Chen")])
    name, matched = index.canonicalize("Diaz, Ben")
    assert matched is False
    assert name == "Diaz, Ben"


def test_canonicalize_ambiguous_multiple_matches_returns_original_unchanged():
    """Two roster entries sharing enough tokens to both satisfy the subset
    check must not silently pick one — never fabricate a match."""
    index = RosterIndex(
        [
            _entry("Chen, Alice", "Alice", "Chen"),
            _entry("Chen, Alice Marie", "Alice Marie", "Chen"),
        ]
    )
    name, matched = index.canonicalize("Chen, Alice")
    assert matched is False
    assert name == "Chen, Alice"


def test_canonicalize_empty_name_returns_unmatched():
    index = RosterIndex([_entry("Chen, Alice", "Alice", "Chen")])
    name, matched = index.canonicalize("")
    assert matched is False
    assert name == ""


def _entry(canonical_name, first, last):
    from real_schedule.roster import RosterEntry

    return RosterEntry(canonical_name=canonical_name, first=first, last=last)
