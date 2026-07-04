"""Shared constraint definitions for all three solvers.

Single source of truth for ACGME rules and program-specific policies.
full_schedule.py, repair.py, and warm_start.py must import constraints from
here rather than inlining them — see CLAUDE.md's "Conventions & Guardrails".

Development Priority #2 (CLAUDE.md).
"""

from __future__ import annotations


def duty_hour_constraints(*args, **kwargs):
    raise NotImplementedError("solver/rules.py: Development Priority #2")


def no_double_coverage_constraint(*args, **kwargs):
    raise NotImplementedError("solver/rules.py: Development Priority #2")


def required_rotation_constraint(*args, **kwargs):
    raise NotImplementedError("solver/rules.py: Development Priority #2")


def vacation_respect_constraint(*args, **kwargs):
    raise NotImplementedError("solver/rules.py: Development Priority #2")
