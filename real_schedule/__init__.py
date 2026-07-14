"""Read-only readers and checkers over the real, live schedule workbooks at
Resident_Schedules/ (real PII, synced from Duke OneDrive).

This package has no relationship to db.models/solver — the people here
don't have synthetic IDs, everything is name-keyed, and nothing in this
package is ever written back to those workbooks. Every reader opens with
openpyxl.load_workbook(path, read_only=True, data_only=True) — read_only
mode removes the write API from the object entirely, which is the actual
enforcement mechanism for "never write to Resident_Schedules/", not just a
comment. No module in this package imports the write-capable
openpyxl.Workbook() constructor, and none has a save_*/write_* function —
not unused, absent.

See CLAUDE.md's PII boundary section for the (deliberate, chief-resident-
approved) policy this package operates under.
"""
