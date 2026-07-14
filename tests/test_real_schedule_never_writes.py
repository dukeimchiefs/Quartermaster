"""Structural regression guard: real_schedule/ must never be able to write
to Resident_Schedules/. This asserts the negative property directly rather
than just trusting code review — matching the existing project pattern in
tests/test_network_offline_guard.py (asserting a negative property about
the codebase, not just positive behavior).
"""

from __future__ import annotations

import ast
from pathlib import Path

import real_schedule
from real_schedule import ambulatory, assist_list, available_clinics, checks, common, master_schedule

_MODULES = [common, master_schedule, ambulatory, assist_list, available_clinics, checks]
_FORBIDDEN_CALLABLE_PREFIXES = ("save", "write")


def test_no_module_exposes_a_save_or_write_callable():
    for module in _MODULES:
        offending = [
            name
            for name in dir(module)
            if callable(getattr(module, name))
            and any(name.lower().startswith(prefix) for prefix in _FORBIDDEN_CALLABLE_PREFIXES)
        ]
        assert offending == [], f"{module.__name__} exposes a save/write-named callable: {offending}"


def test_no_module_imports_the_write_capable_workbook_constructor():
    """openpyxl.Workbook() (no arguments) is the write-capable constructor
    used to CREATE a new workbook from scratch — real_schedule/ readers
    should only ever call openpyxl.load_workbook(..., read_only=True), which
    returns an object with no .save() available at all. Source-scan for any
    module importing the write constructor directly."""
    package_dir = Path(real_schedule.__file__).parent
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "openpyxl":
                names = {alias.name for alias in node.names}
                assert "Workbook" not in names, f"{path.name} imports the write-capable openpyxl.Workbook constructor"


def test_every_load_workbook_call_uses_read_only():
    """Source-scan every real_schedule/*.py file's load_workbook(...) calls
    and confirm read_only=True is always passed — this is the actual
    enforcement mechanism (read_only mode has no .save() at all), not just
    a comment."""
    package_dir = Path(real_schedule.__file__).parent
    checked_any = False
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "load_workbook"
            ):
                checked_any = True
                kwarg_names = {kw.arg: kw.value for kw in node.keywords}
                assert "read_only" in kwarg_names, f"{path.name}: load_workbook() call missing read_only kwarg"
                value = kwarg_names["read_only"]
                assert isinstance(value, ast.Constant) and value.value is True, (
                    f"{path.name}: load_workbook() called with read_only != True"
                )
    assert checked_any, "expected to find at least one load_workbook() call to check"
