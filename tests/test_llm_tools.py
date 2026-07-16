"""Tests for llm/client.py and llm/tools.py — Development Priority #5
("Ollama integration — single prompt, single tool") and #6 ("LLM-driven
call-out parsing — free-text → structured solver call") from CLAUDE.md.
These tests never talk to a real Ollama server; recommend_swaps and
handle_callout_message take a fake client double so the suite runs without
the (heavy, optional) Ollama install.

The core guarantee under test: no matter what the fake LLM does — calls a
tool, skips it, narrates a resident_id a tool never returned, or fails to
resolve who/when — the candidate set that comes back is always exactly what
solver.repair.repair_schedule produced, or nothing at all (CLAUDE.md's
"solver produces the candidates, the LLM only ranks and narrates them").
handle_callout_message's ID hand-off between query_schedule_db and
call_repair_solver happens in plain Python rather than via a second model
turn — see that function's docstring for why (verified live against
llama3.1:8b: with both tools registered at once, it would sometimes fire a
premature call_repair_solver with hallucinated IDs in the same turn as the
lookup).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

import pytest

from llm.client import DEFAULT_HOST, OllamaClient, RemoteInferenceBlocked
from llm.tools import (
    _ambiguous_match_reply,
    _match_names,
    call_repair_solver,
    handle_callout_message,
    query_schedule_db,
    recommend_swaps,
)
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


# --- llm/tools.py: query_schedule_db -----------------------------------------


def test_query_schedule_db_matches_name_and_date(schedule):
    matches = query_schedule_db(schedule, name="alice", date="2026-07-10")

    assert len(matches) == 1
    assert matches[0]["resident_id"] == 1
    assert matches[0]["block_id"] == 1
    assert matches[0]["rotation_id"] == 1
    assert matches[0]["role"] == "senior"


def test_query_schedule_db_no_match_for_unknown_name(schedule):
    assert query_schedule_db(schedule, name="nobody", date="2026-07-10") == []


def test_query_schedule_db_no_match_outside_block_date_range(schedule):
    assert query_schedule_db(schedule, name="alice", date="2026-09-01") == []


# --- llm/tools.py: _match_names / _ambiguous_match_reply ---------------------


def test_match_names_resolves_nickname_across_last_first_order():
    """Regression test for the "Chris Choi" bug: the real roster stores
    "Last, First" names ("Choi, Christopher"); free text says "Chris Choi"
    (First Last, and a nickname). A plain substring check finds nothing —
    token+prefix matching must."""
    names = ["Choi, Christopher", "Delaney, Christopher", "Gitter, Christopher", "Norberg, Chris"]
    assert _match_names("Chris Choi", names) == ["Choi, Christopher"]


def test_match_names_single_token_can_be_ambiguous():
    names = ["Choi, Christopher", "Norberg, Chris"]
    assert _match_names("Chris", names) == ["Choi, Christopher", "Norberg, Chris"]


def test_match_names_no_match_returns_empty():
    assert _match_names("Zzz Nobody", ["Choi, Christopher"]) == []


def test_match_names_blank_query_returns_empty():
    assert _match_names("", ["Choi, Christopher"]) == []
    assert _match_names(None, ["Choi, Christopher"]) == []


def test_ambiguous_match_reply_lists_real_matches_never_invents():
    reply = _ambiguous_match_reply("resident", "Chris Choi", ["Choi, Christopher", "Choi, Christine"])
    assert "Choi, Christopher" in reply
    assert "Choi, Christine" in reply


def test_ambiguous_match_reply_zero_matches_says_so_plainly():
    reply = _ambiguous_match_reply("resident", "Zzz Nobody", [])
    assert "Zzz Nobody" in reply
    assert "couldn't find" in reply.lower()


# --- llm/tools.py: handle_callout_message ------------------------------------


class _FakeCalloutClientResolves:
    """Simulates a model that resolves the lookup on the first try, then
    narrates the solver's candidates on the second (no-tools) turn."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, *, tools=None, format=None, **kwargs):
        self.calls += 1
        if tools:
            call = _FakeToolCall(
                function=_FakeFunction(name="query_schedule_db", arguments={"name": "Alice", "date": "2026-07-10"})
            )
            return _FakeResponse(message=_FakeMessage(tool_calls=[call]))
        narratives = {
            "narratives": [
                {"resident_id": 3, "narrative": "Carla has the lightest load."},
                {"resident_id": 2, "narrative": "Brian is next best."},
            ]
        }
        return _FakeResponse(message=_FakeMessage(content=json.dumps(narratives)))


class _FakeCalloutClientAmbiguousName:
    """Simulates a model whose lookup resolves to zero matches, then asks a
    clarifying question when told so."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, *, tools=None, format=None, **kwargs):
        self.calls += 1
        if tools:
            call = _FakeToolCall(
                function=_FakeFunction(name="query_schedule_db", arguments={"name": "Zzz", "date": "2026-07-10"})
            )
            return _FakeResponse(message=_FakeMessage(tool_calls=[call]))
        return _FakeResponse(message=_FakeMessage(content="Who exactly do you mean, and what date?"))


class _FakeCalloutClientNeverCallsTool:
    """Simulates a model that responds with plain text instead of resolving
    who/when at all (e.g. the message was too vague)."""

    def chat(self, messages, *, tools=None, format=None, **kwargs):
        return _FakeResponse(message=_FakeMessage(content="Who's out, and on what date?"))


def test_handle_callout_message_resolves_and_finds_coverage(schedule):
    result = handle_callout_message(
        schedule, "Alice is out", today=dt.date(2026, 7, 10), client=_FakeCalloutClientResolves()
    )

    assert result.resolved is True
    assert result.sick_resident_id == 1
    assert [p.resident_id for p in result.proposals] == [3, 2]
    assert result.proposals[0].narrative == "Carla has the lightest load."


def test_handle_callout_message_asks_to_clarify_on_zero_matches(schedule):
    result = handle_callout_message(
        schedule, "Zzz is out", today=dt.date(2026, 7, 10), client=_FakeCalloutClientAmbiguousName()
    )

    assert result.resolved is False
    assert result.proposals is None
    assert "?" in result.reply


def test_handle_callout_message_asks_to_clarify_when_model_never_looks_up(schedule):
    result = handle_callout_message(
        schedule, "someone's out", today=dt.date(2026, 7, 10), client=_FakeCalloutClientNeverCallsTool()
    )

    assert result.resolved is False
    assert result.proposals is None
