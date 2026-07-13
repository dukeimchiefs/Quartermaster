"""Development Priority #12 (CLAUDE.md) — "offline check". CLAUDE.md's
Non-Negotiable Privacy & Governance Constraints require "no cloud inference,
no external APIs, no telemetry" and that every dependency be audited for
phone-home behavior. requirements.txt documents that audit per-dependency,
but a comment is only a claim about the code as it existed when written —
dependencies get upgraded, code paths change. This test is the enforcement:
it patches socket.create_connection (the low-level choke point urllib3,
httpx, and everything built on them eventually calls) to raise on any
destination that isn't loopback, then actually exercises the app's real
code paths underneath that guard — including a live call through
llm.client.OllamaClient to the local Ollama server, not a mock. If any
dependency starts dialing out, this fails loudly instead of silently.
"""

from __future__ import annotations

import socket

import pytest

from db.models import Assignment, Block, CallHistory, Resident, Rotation, TimeOff, get_engine, get_session, init_db
from db.seed import seed
from llm.client import DEFAULT_MODEL, OllamaClient
from solver.repair import CurrentSchedule, OpenShift, repair_schedule

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _ollama_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture
def block_non_loopback(monkeypatch):
    real_create_connection = socket.create_connection

    def guarded(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK_HOSTS and not str(host).startswith("127."):
            raise AssertionError(f"Blocked an attempted network connection to non-loopback host {host!r}")
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guarded)


@pytest.fixture
def seeded_schedule():
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    with get_session(engine) as session:
        seed(session)
        yield CurrentSchedule(
            assignments=session.query(Assignment).all(),
            residents=session.query(Resident).all(),
            rotations=session.query(Rotation).all(),
            blocks=session.query(Block).all(),
            time_off=session.query(TimeOff).all(),
            call_history=session.query(CallHistory).all(),
        )


def test_repair_solver_makes_no_network_connections(block_non_loopback, seeded_schedule):
    """CP-SAT is a native local solver — this is the regression guard for
    requirements.txt's ortools audit note staying true as the dependency is
    upgraded."""
    sick = next(r for r in seeded_schedule.residents if r.name == "Elena Petrov")
    wards_id = next(r.id for r in seeded_schedule.rotations if r.name == "Wards")
    block_1_id = next(b.id for b in seeded_schedule.blocks if b.block_number == 1)

    import datetime as dt

    open_shift = OpenShift(
        block_id=block_1_id, rotation_id=wards_id, role="senior", date=dt.date(2026, 7, 10),
        shift_type="night_call", hours=14.0,
    )
    proposals = repair_schedule(seeded_schedule, open_shift, sick_resident=sick.id)
    assert proposals  # sanity: the guard didn't just eat a real failure


@pytest.mark.skipif(not _ollama_reachable(), reason="Ollama server not running locally on 127.0.0.1:11434")
def test_ollama_client_only_ever_talks_to_loopback(block_non_loopback):
    """Proves llm.client.OllamaClient's inference traffic never leaves the
    box, against the real local model — not a mock standing in for the
    claim."""
    client = OllamaClient(model=DEFAULT_MODEL)
    response = client.chat(messages=[{"role": "user", "content": "Reply with the single word: ok"}])
    assert response.message.content
