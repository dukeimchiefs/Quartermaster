"""Function-calling interface exposed to the local LLM: solvers, DB queries,
and the rule explainer.

The LLM must never generate schedule assignments directly or be the final
word on legality — every tool call here that affects DB state round-trips
through a solver in solver/ for validation. See CLAUDE.md's "What the LLM
must not do".

Development Priority #5 / #6 (CLAUDE.md).
"""

from __future__ import annotations


def call_repair_solver(*args, **kwargs):
    raise NotImplementedError("llm/tools.py: Development Priority #5")


def query_schedule_db(*args, **kwargs):
    raise NotImplementedError("llm/tools.py: Development Priority #5")


def explain_rule_violation(*args, **kwargs):
    raise NotImplementedError("llm/tools.py: Development Priority #6")
