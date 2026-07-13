"""Function-calling interface exposed to the local LLM: solvers, DB queries,
and the rule explainer.

The LLM must never generate schedule assignments directly or be the final
word on legality — every tool call here that affects DB state round-trips
through a solver in solver/ for validation. See CLAUDE.md's "What the LLM
must not do".

Development Priority #5 / #6 (CLAUDE.md). Priority #5 is scoped to a single
prompt (llm/prompts/callout_handler.md) and a single tool
(call_repair_solver) — query_schedule_db and explain_rule_violation are
Priority #6 and remain stubs.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from llm.client import DEFAULT_MODEL, OllamaClient
from solver.repair import CurrentSchedule, OpenShift, SwapProposal, repair_schedule

_PROMPT_PATH = Path(__file__).parent / "prompts" / "callout_handler.md"

CALL_REPAIR_SOLVER_TOOL = {
    "type": "function",
    "function": {
        "name": "call_repair_solver",
        "description": (
            "Find replacement coverage for an open shift by running the "
            "call-out repair solver against the real schedule. Returns "
            "candidates ranked from least to most burdened, or an empty "
            "list if nobody is feasible. This is the only source of truth "
            "for who can cover a shift."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "block_id": {"type": "integer", "description": "Block ID of the open shift."},
                "rotation_id": {"type": "integer", "description": "Rotation ID of the open shift."},
                "role": {"type": "string", "enum": ["intern", "senior"]},
                "date": {"type": "string", "description": "ISO date (YYYY-MM-DD) of the shift."},
                "shift_type": {"type": "string", "description": "e.g. 'night_call'."},
                "hours": {"type": "number", "description": "Duration of the shift in hours."},
                "sick_resident_id": {"type": "integer", "description": "Resident ID who is out."},
            },
            "required": [
                "block_id",
                "rotation_id",
                "role",
                "date",
                "shift_type",
                "hours",
                "sick_resident_id",
            ],
        },
    },
}

_NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "narratives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "resident_id": {"type": "integer"},
                    "narrative": {"type": "string"},
                },
                "required": ["resident_id", "narrative"],
            },
        }
    },
    "required": ["narratives"],
}


@dataclass
class RankedSwapProposal:
    resident_id: int
    resident_name: str
    rank: int
    projected_window_hours: float
    narrative: str


def call_repair_solver(
    current_schedule: CurrentSchedule,
    *,
    block_id: int,
    rotation_id: int,
    role: str,
    date: str | dt.date,
    shift_type: str,
    hours: float,
    sick_resident_id: int,
) -> list[dict]:
    """The single tool exposed to the LLM (Priority #5). A thin,
    JSON-in/JSON-out wrapper around solver.repair.repair_schedule — the LLM
    never computes eligibility, hours, or ranking itself, it only reads
    this tool's return value back.
    """
    open_shift = OpenShift(
        block_id=block_id,
        rotation_id=rotation_id,
        role=role,
        date=dt.date.fromisoformat(date) if isinstance(date, str) else date,
        shift_type=shift_type,
        hours=hours,
    )
    proposals = repair_schedule(current_schedule, open_shift, sick_resident=sick_resident_id)
    residents_by_id = {r.id: r for r in current_schedule.residents}
    return [
        {
            "resident_id": p.resident_id,
            "resident_name": residents_by_id[p.resident_id].name,
            "rank": p.rank,
            "projected_window_hours": p.projected_window_hours,
            "solver_reason": p.reason,
        }
        for p in proposals
    ]


def recommend_swaps(
    current_schedule: CurrentSchedule,
    open_shift: OpenShift,
    sick_resident: int,
    candidates: list[SwapProposal] | None = None,
    *,
    client: OllamaClient | None = None,
) -> list[RankedSwapProposal]:
    """CLAUDE.md's chief-facing "what's my best option here" entry point —
    not a fourth solver. Runs the actual Ollama tool-calling round trip
    (Priority #5's "single prompt, single tool") so the model itself
    decides to call call_repair_solver, then narrates the result in prose.

    The solver stays authoritative regardless of what the model does: if it
    skips calling the tool, we call it directly; if it narrates a
    resident_id the tool never returned, that narration is dropped. Either
    way the returned candidate set is exactly what the solver produced.
    """
    tool_result = candidates and [
        {
            "resident_id": p.resident_id,
            "resident_name": next(r.name for r in current_schedule.residents if r.id == p.resident_id),
            "rank": p.rank,
            "projected_window_hours": p.projected_window_hours,
            "solver_reason": p.reason,
        }
        for p in candidates
    ]

    system_prompt = _PROMPT_PATH.read_text()
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Resident {sick_resident} is out for a {open_shift.shift_type} "
                f"shift on {open_shift.date.isoformat()} (block {open_shift.block_id}, "
                f"rotation {open_shift.rotation_id}, role '{open_shift.role}', "
                f"{open_shift.hours}h). Find and rank coverage options."
            ),
        },
    ]

    client = client or OllamaClient(model=DEFAULT_MODEL)

    if tool_result is None:
        response = client.chat(messages=messages, tools=[CALL_REPAIR_SOLVER_TOOL])
        messages.append(response.message.model_dump())

        for call in response.message.tool_calls or []:
            if call.function.name != "call_repair_solver":
                continue  # single-tool scope (Priority #5) — ignore anything else
            tool_result = call_repair_solver(current_schedule, **call.function.arguments)
            messages.append(
                {"role": "tool", "content": json.dumps(tool_result), "tool_name": call.function.name}
            )
            break

    if tool_result is None:
        # Model didn't call its one tool — fall back to calling the solver
        # directly rather than returning nothing just because the LLM
        # skipped its one job. The solver stays authoritative either way.
        tool_result = call_repair_solver(
            current_schedule,
            block_id=open_shift.block_id,
            rotation_id=open_shift.rotation_id,
            role=open_shift.role,
            date=open_shift.date.isoformat(),
            shift_type=open_shift.shift_type,
            hours=open_shift.hours,
            sick_resident_id=sick_resident,
        )

    if not tool_result:
        return []

    valid_ids = {c["resident_id"] for c in tool_result}
    narratives: dict[int, str] = {}
    try:
        narration_response = client.chat(
            messages=messages
            + [
                {
                    "role": "user",
                    "content": (
                        "Now write a one-sentence, chief-resident-facing rationale "
                        "for each candidate above, in rank order. Use only the "
                        "candidates given — do not add or remove any."
                    ),
                }
            ],
            format=_NARRATIVE_SCHEMA,
        )
        parsed = json.loads(narration_response.message.content)
        narratives = {n["resident_id"]: n["narrative"] for n in parsed["narratives"] if n["resident_id"] in valid_ids}
    except (json.JSONDecodeError, KeyError, TypeError):
        pass  # fall back to the solver's own reason string below

    return [
        RankedSwapProposal(
            resident_id=c["resident_id"],
            resident_name=c["resident_name"],
            rank=c["rank"],
            projected_window_hours=c["projected_window_hours"],
            narrative=narratives.get(c["resident_id"], c["solver_reason"]),
        )
        for c in tool_result
    ]


def query_schedule_db(*args, **kwargs):
    raise NotImplementedError("llm/tools.py: Development Priority #6")


def explain_rule_violation(*args, **kwargs):
    raise NotImplementedError("llm/tools.py: Development Priority #6")
