"""Append-only audit logger.

Every schedule change the system proposes or commits must be written here
before returning to the caller — timestamp, actor, and reason. This is the
only sink permitted to contain PII in production mode. See CLAUDE.md's
Non-Negotiable Privacy & Governance Constraints.

Development Priority #7 (CLAUDE.md).
"""

from __future__ import annotations


def record(actor: str, action: str, reason: str, details: str | None = None):
    raise NotImplementedError("audit/log.py: Development Priority #7")
