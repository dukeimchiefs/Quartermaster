"""Tests for llm/client.py and llm/tools.py — Development Priority #5
(CLAUDE.md): "Ollama integration — single prompt, single tool (the repair
solver)". These tests never talk to a real Ollama server; recommend_swaps
takes a fake client double so the suite runs without the (heavy, optional)
Ollama install.

The core guarantee under test: no matter what the fake LLM does — calls the
tool, skips it, or narrates a resident_id the tool never returned — the
candidate set recommend_swaps returns is always exactly what
solver.repair.repair_schedule produced (CLAUDE.md's "solver produces the
candidates, the LLM only ranks and narrates them").
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

import pytest

from llm.client import DEFAULT_HOST, OllamaClient, RemoteInferenceBlocked
from llm.tools import call_repair_solver, recommend_swaps
from solver.repair import CurrentSchedule, OpenShift


@dataclass
class FakeResident:
    id: int
    name: str
    pgy: int = 2


@dataclass
class FakeRotation:
    id: int
    name: str
    intern_capacity: int = 2
    senior_capacity: int = 2
    requires_pgy: int | None = None


@dataclass
class FakeBlock:
    id: int
    block_number: int
    start_date: dt.date
    end_date: dt.date


@dataclass
class FakeAssignment:
    resident_id: int
    block_id: int
    rotation_id: int
    role: str


@dataclass
class FakeCallHistory:
    resident_id: int
    date: dt.date
    shift_type: str
    hours: float


@pytest.fixture
def schedule() -> CurrentSchedule:
    return CurrentSchedule(
        assignments=[
            FakeAssignment(resident_id=1, block_id=1, rotation_id=1, role="senior"),  # sick
            FakeAssignment(resident_id=2, block_id=1, rotation_id=1, role="senior"),
            FakeAssignment(resident_id=3, block_id=1, rotation_id=1, role="senior"),
        ],
        residents=[
            FakeResident(id=1, name="Alice Chen"),
            FakeResident(id=2, name="Brian Osei"),
            FakeResident(id=3, name="Carla Nguyen"),
        ],
        rotations=[FakeRotation(id=1, name="ICU")],
        blocks=[FakeBlock(id=1, block_number=1, start_date=dt.date(2026, 7, 1), end_date=dt.date(2026, 7, 28))],
        time_off=[],
        call_history=[],
    )


@pytest.fixture
def open_shift() -> OpenShift:
    return OpenShift(
        block_id=1, rotation_id=1, role="senior", date=dt.date(2026, 7, 10), shift_type="night_call", hours=14.0
    )


# --- llm/client.py guards ---------------------------------------------------


def test_ollama_client_rejects_non_loopback_host():
    with pytest.raises(RemoteInferenceBlocked):
        OllamaClient(model="llama3.1:8b", host="http://example.com:11434")


def test_ollama_client_rejects_cloud_model_tag():
    with pytest.raises(RemoteInferenceBlocked):
        OllamaClient(model="llama3.1:70b-cloud", host=DEFAULT_HOST)


def test_ollama_client_accepts_loopback_host_and_local_tag():
    OllamaClient(model="llama3.1:8b", host=DEFAULT_HOST)  # must not raise


# --- llm/tools.py: call_repair_solver ---------------------------------------


def test_call_repair_solver_resolves_names_and_matches_solver_output(schedule):
    result = call_repair_solver(
        schedule,
        block_id=1,
        rotation_id=1,
        role="senior",
        date="2026-07-10",
        shift_type="night_call",
        hours=14.0,
        sick_resident_id=1,
    )

    assert [c["resident_name"] for c in result] == ["Carla Nguyen", "Brian Osei"]
    assert all("solver_reason" in c for c in result)


# --- llm/tools.py: recommend_swaps ------------------------------------------


@dataclass
class _FakeFunction:
    name: str
    arguments: dict


@dataclass
class _FakeToolCall:
    function: _FakeFunction


@dataclass
class _FakeMessage:
    content: str = ""
    tool_calls: list = field(default_factory=list)

    def model_dump(self):
        return {"role": "assistant", "content": self.content, "tool_calls": self.tool_calls}


@dataclass
class _FakeResponse:
    message: _FakeMessage


class _FakeClientCallsTool:
    """Simulates a model that calls call_repair_solver, then narrates."""

    def chat(self, messages, *, tools=None, format=None, **kwargs):
        if tools:  # first turn: emit the tool call
            call = _FakeToolCall(
                function=_FakeFunction(
                    name="call_repair_solver",
                    arguments={
                        "block_id": 1,
                        "rotation_id": 1,
                        "role": "senior",
                        "date": "2026-07-10",
                        "shift_type": "night_call",
                        "hours": 14.0,
                        "sick_resident_id": 1,
                    },
                )
            )
            return _FakeResponse(message=_FakeMessage(tool_calls=[call]))
        # second turn: narration, including a hallucinated resident_id=999
        narratives = {
            "narratives": [
                {"resident_id": 3, "narrative": "Carla has the lightest load."},
                {"resident_id": 2, "narrative": "Brian is next best."},
                {"resident_id": 999, "narrative": "A candidate that doesn't exist."},
            ]
        }
        return _FakeResponse(message=_FakeMessage(content=json.dumps(narratives)))


class _FakeClientSkipsTool:
    """Simulates a model that never calls the tool at all."""

    def chat(self, messages, *, tools=None, format=None, **kwargs):
        if tools:
            return _FakeResponse(message=_FakeMessage(tool_calls=[]))
        return _FakeResponse(message=_FakeMessage(content=json.dumps({"narratives": []})))


def test_recommend_swaps_uses_tool_call_and_drops_hallucinated_candidate(schedule, open_shift):
    results = recommend_swaps(schedule, open_shift, sick_resident=1, client=_FakeClientCallsTool())

    assert [r.resident_id for r in results] == [3, 2]
    assert results[0].narrative == "Carla has the lightest load."
    assert results[1].narrative == "Brian is next best."


def test_recommend_swaps_falls_back_to_solver_when_model_skips_tool(schedule, open_shift):
    """Even if the fake model never calls call_repair_solver, the solver's
    candidate set must still come back — the LLM is narration-only, never
    a gatekeeper for whether candidates are found at all."""
    results = recommend_swaps(schedule, open_shift, sick_resident=1, client=_FakeClientSkipsTool())

    assert [r.resident_id for r in results] == [3, 2]
    # no narratives matched (empty from the fake) -> falls back to solver_reason
    assert "resident 3" in results[0].narrative
