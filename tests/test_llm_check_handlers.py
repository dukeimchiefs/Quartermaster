"""Tests for the four handle_check_*_message() free-text handlers in
llm/tools.py — same fake-client-double convention as tests/test_llm_tools.py
(no real Ollama server involved). The core guarantee under test: the model
only ever resolves identifying fields via one tool call and narrates a
result it's handed — the actual checker output (real_schedule/checks.py /
real_schedule/recommend.py) is always what comes back, and ambiguous or
unresolvable fields always lead to a clarifying question, never a guess.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from real_schedule.ambulatory import AmbulatoryWeekRow
from real_schedule.assist_list import AssistWeekEntry, MasterAssistDuty
from real_schedule.available_clinics import ClinicSlot
from real_schedule.fsc_tracker import FscBalance
from real_schedule.master_schedule import MasterScheduleWeek
from llm.tools import (
    handle_check_assist_swap_message,
    handle_check_clinic_coverage_message,
    handle_check_fsc_reflection_message,
    handle_check_rotation_swap_message,
)

TODAY = dt.date(2026, 7, 6)
WEEK_1 = dt.date(2026, 7, 6)
WEEK_2 = dt.date(2026, 7, 13)


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


class _FakeClientResolves:
    """Emits the given tool call on the first (tools=) turn, plain-text
    narration on every later turn."""

    def __init__(self, tool_name: str, arguments: dict, narration: str = "Looks fine."):
        self.tool_name = tool_name
        self.arguments = arguments
        self.narration = narration

    def chat(self, messages, *, tools=None, format=None, **kwargs):
        if tools:
            call = _FakeToolCall(function=_FakeFunction(name=self.tool_name, arguments=self.arguments))
            return _FakeResponse(message=_FakeMessage(tool_calls=[call]))
        return _FakeResponse(message=_FakeMessage(content=self.narration))


class _FakeClientNeverCallsTool:
    def chat(self, messages, *, tools=None, format=None, **kwargs):
        return _FakeResponse(message=_FakeMessage(content="Who exactly do you mean?"))


# --- handle_check_rotation_swap_message --------------------------------------


def test_rotation_swap_resolves_and_runs_checker():
    master_schedule = [
        MasterScheduleWeek(resident_name="Chen, Alice", pgy=2, week_start=WEEK_1, rotation="VA GM"),
        MasterScheduleWeek(resident_name="Osei, Brian", pgy=2, week_start=WEEK_1, rotation="AMB Endo"),
        MasterScheduleWeek(resident_name="Chen, Alice", pgy=2, week_start=WEEK_2, rotation="VA GM"),
        MasterScheduleWeek(resident_name="Osei, Brian", pgy=2, week_start=WEEK_2, rotation="AMB Endo"),
    ]
    client = _FakeClientResolves(
        "resolve_rotation_swap_request",
        {"resident_1": "Alice", "resident_2": "Brian", "week_start_first": "2026-07-06", "week_start_last": "2026-07-13"},
        narration="No blocking issues.",
    )
    result = handle_check_rotation_swap_message(master_schedule, master_assist=[], free_text="Alice and Brian want to swap", today=TODAY, client=client)

    assert result.resolved is True
    assert result.result.is_clear
    assert result.reply == "No blocking issues."


def test_rotation_swap_asks_to_clarify_on_ambiguous_name():
    master_schedule = [
        MasterScheduleWeek(resident_name="Chen, Alice", pgy=2, week_start=WEEK_1, rotation="VA GM"),
        MasterScheduleWeek(resident_name="Chen, Andrew", pgy=2, week_start=WEEK_1, rotation="AMB Endo"),
    ]
    client = _FakeClientResolves(
        "resolve_rotation_swap_request",
        {"resident_1": "Chen", "resident_2": "Chen", "week_start_first": "2026-07-06", "week_start_last": "2026-07-06"},
    )
    result = handle_check_rotation_swap_message(master_schedule, master_assist=[], free_text="Chen and Chen swap", today=TODAY, client=client)

    assert result.resolved is False
    assert result.result is None


def test_rotation_swap_asks_to_clarify_when_model_never_calls_tool():
    result = handle_check_rotation_swap_message([], master_assist=[], free_text="someone wants a swap", today=TODAY, client=_FakeClientNeverCallsTool())
    assert result.resolved is False


# --- handle_check_assist_swap_message -----------------------------------------


def test_assist_swap_resolves_and_runs_checker():
    master_assist = [
        MasterAssistDuty(resident_name="Chen, Alice", pgy_tier="PGY-2", week_start=WEEK_1, duty="JEOPARDY", extra=None),
        MasterAssistDuty(resident_name="Osei, Brian", pgy_tier="PGY-2", week_start=WEEK_1, duty="", extra=None),
    ]
    weekly_assist = [
        AssistWeekEntry(resident_name="Chen, Alice", pgy=2, rotation="JEOPARDY", week_start=WEEK_1, pulls_this_year=1.0, day_parts={}),
        AssistWeekEntry(resident_name="Chen, Alice", pgy=2, rotation="OFF", week_start=WEEK_2, pulls_this_year=1.0, day_parts={}),
    ]
    master_schedule = [
        MasterScheduleWeek(resident_name="Osei, Brian", pgy=2, week_start=WEEK_1, rotation="OFF"),
        MasterScheduleWeek(resident_name="Chen, Alice", pgy=2, week_start=WEEK_2, rotation="OFF"),
    ]
    client = _FakeClientResolves(
        "resolve_assist_swap_request",
        {"resident_1": "Alice", "resident_2": "Brian", "week_covered": "2026-07-06", "week_new": "2026-07-13"},
        narration="Clean swap.",
    )
    result = handle_check_assist_swap_message(
        master_assist, weekly_assist, master_schedule, free_text="Alice and Brian swap jeopardy weeks", today=TODAY, client=client
    )

    assert result.resolved is True
    assert result.result.is_clear
    assert result.reply == "Clean swap."


def test_assist_swap_asks_to_clarify_when_week_unresolvable():
    master_assist = [MasterAssistDuty(resident_name="Chen, Alice", pgy_tier="PGY-2", week_start=WEEK_1, duty="JEOPARDY", extra=None)]
    weekly_assist = [AssistWeekEntry(resident_name="Chen, Alice", pgy=2, rotation="JEOPARDY", week_start=WEEK_1, pulls_this_year=1.0, day_parts={})]
    client = _FakeClientResolves(
        "resolve_assist_swap_request",
        {"resident_1": "Chen", "resident_2": "Chen", "week_covered": "2099-01-01", "week_new": "2099-01-08"},
    )
    result = handle_check_assist_swap_message(master_assist, weekly_assist, [], free_text="bogus", today=TODAY, client=client)
    assert result.resolved is False


# --- handle_check_fsc_reflection_message --------------------------------------


def test_fsc_reflection_resolves_and_runs_checker():
    master_schedule = [MasterScheduleWeek(resident_name="Chen, Alice", pgy=2, week_start=WEEK_1, rotation="AMB Endo")]
    ambulatory_week = [AmbulatoryWeekRow(resident_name="Chen, Alice", pgy=2, rotation="AMB Endo", day_parts={(WEEK_1, "AM"): "Dr. X\n(SD)"})]
    fsc_balances = [FscBalance(resident_name="Chen, Alice", pgy=2, program="Categorical", base_fsc=4, fsc_available=4, fsc_used=0, fsc_left=4, phase=None)]
    client = _FakeClientResolves(
        "resolve_fsc_reflection_request",
        {"resident": "Alice", "date": "2026-07-06", "portion": "AM"},
        narration="Eligible.",
    )
    result = handle_check_fsc_reflection_message(
        master_schedule, master_assist=[], ambulatory_week=ambulatory_week, fsc_balances=fsc_balances,
        free_text="Can Alice take Monday AM off?", today=TODAY, client=client,
    )

    assert result.resolved is True
    assert result.reply == "Eligible."


def test_fsc_reflection_asks_to_clarify_on_date_outside_loaded_week():
    master_schedule = [MasterScheduleWeek(resident_name="Chen, Alice", pgy=2, week_start=WEEK_1, rotation="AMB Endo")]
    ambulatory_week = [AmbulatoryWeekRow(resident_name="Chen, Alice", pgy=2, rotation="AMB Endo", day_parts={(WEEK_1, "AM"): "Dr. X\n(SD)"})]
    client = _FakeClientResolves(
        "resolve_fsc_reflection_request",
        {"resident": "Alice", "date": "2030-01-01", "portion": "AM"},
    )
    result = handle_check_fsc_reflection_message(
        master_schedule, master_assist=[], ambulatory_week=ambulatory_week, fsc_balances=[],
        free_text="bogus", today=TODAY, client=client,
    )
    assert result.resolved is False


# --- handle_check_clinic_coverage_message -------------------------------------


def test_clinic_coverage_resolves_and_returns_ranked_candidates():
    ambulatory_week = [
        AmbulatoryWeekRow(resident_name="Delta, Fictional", pgy=1, rotation="AMB ID", day_parts={(WEEK_1, "AM"): "Dr. Out\n(SD)"}),
    ]
    available_clinics = [
        ClinicSlot(preceptor_name="Dr. Out", specialty="ID", location="SD", tier="Intern Blocks", available_day_parts={("Mon", "AM")}),
        ClinicSlot(
            preceptor_name="Dr. Available", specialty="ID", location="SD", tier="Intern Blocks",
            available_day_parts={("Mon", "AM")}, site_codes_by_day_part={("Mon", "AM"): "SD"},
        ),
    ]
    client = _FakeClientResolves(
        "resolve_clinic_coverage_request",
        {"called_out_preceptor": "Dr. Out", "date": "2026-07-06", "half_day": "AM"},
        narration="Dr. Available can cover.",
    )
    result = handle_check_clinic_coverage_message(
        ambulatory_week, available_clinics, free_text="Dr. Out called out Monday AM", today=TODAY, client=client
    )

    assert result.resolved is True
    assert [c.preceptor_name for c in result.result] == ["Dr. Available"]
    assert result.reply == "Dr. Available can cover."


def test_clinic_coverage_asks_to_clarify_on_ambiguous_preceptor():
    ambulatory_week = [
        AmbulatoryWeekRow(resident_name="Delta, Fictional", pgy=1, rotation="AMB ID", day_parts={(WEEK_1, "AM"): "Dr. Out One\n(SD)", (WEEK_1, "PM"): "Dr. Out Two\n(SD)"}),
    ]
    client = _FakeClientResolves(
        "resolve_clinic_coverage_request",
        {"called_out_preceptor": "Dr. Out", "date": "2026-07-06", "half_day": "AM"},
    )
    result = handle_check_clinic_coverage_message(ambulatory_week, [], free_text="bogus", today=TODAY, client=client)
    assert result.resolved is False
