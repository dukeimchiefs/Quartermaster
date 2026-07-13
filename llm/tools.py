"""Function-calling interface exposed to the local LLM: solvers, DB queries,
and the rule explainer.

The LLM must never generate schedule assignments directly or be the final
word on legality — every tool call here that affects DB state round-trips
through a solver in solver/ for validation. See CLAUDE.md's "What the LLM
must not do".

Development Priority #5 / #6 (CLAUDE.md). Priority #5 wired a single prompt
(llm/prompts/callout_handler.md) and a single tool (call_repair_solver).
Priority #6 adds a second tool, query_schedule_db, and
handle_callout_message() — free text -> structured solver call — which
runs both tools in a multi-turn loop. explain_rule_violation remains a
stub; it isn't needed until infeasibility explanations are in scope.
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

QUERY_SCHEDULE_DB_TOOL = {
    "type": "function",
    "function": {
        "name": "query_schedule_db",
        "description": (
            "Look up which resident(s) matching a name are assigned to a "
            "rotation on a given date, to resolve a free-text call-out into "
            "the block/rotation/role needed to search for coverage. Returns "
            "zero, one, or multiple matches — if it's not exactly one, ask "
            "the chief resident to clarify rather than guessing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Resident's name or partial name, as mentioned by the chief.",
                },
                "date": {"type": "string", "description": "ISO date (YYYY-MM-DD) the resident is out."},
            },
            "required": ["name", "date"],
        },
    },
}

DEFAULT_SHIFT_TYPE = "night_call"
DEFAULT_SHIFT_HOURS = 14.0

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


def query_schedule_db(
    current_schedule: CurrentSchedule,
    *,
    name: str,
    date: str | dt.date,
) -> list[dict]:
    """The second tool exposed to the LLM (Priority #6) — read-only, never
    used to justify a swap by itself. Finds resident(s) whose name contains
    `name` (case-insensitive) with an assignment covering `date`, returning
    enough to build an OpenShift: block, rotation, role. Zero or multiple
    matches is expected and left for the caller (the LLM, per the prompt's
    workflow) to resolve by asking rather than guessing.
    """
    target_date = dt.date.fromisoformat(date) if isinstance(date, str) else date
    name_lower = name.strip().lower()
    blocks_by_id = {b.id: b for b in current_schedule.blocks}
    rotations_by_id = {r.id: r for r in current_schedule.rotations}

    matches = []
    for resident in current_schedule.residents:
        if name_lower not in resident.name.lower():
            continue
        for assignment in current_schedule.assignments:
            if assignment.resident_id != resident.id:
                continue
            block = blocks_by_id.get(assignment.block_id)
            if block is None or not (block.start_date <= target_date <= block.end_date):
                continue
            rotation = rotations_by_id.get(assignment.rotation_id)
            matches.append(
                {
                    "resident_id": resident.id,
                    "resident_name": resident.name,
                    "pgy": resident.pgy,
                    "block_id": block.id,
                    "block_number": block.block_number,
                    "rotation_id": assignment.rotation_id,
                    "rotation_name": rotation.name if rotation else None,
                    "role": assignment.role,
                }
            )
    return matches


@dataclass
class CalloutHandlingResult:
    """Result of handle_callout_message(). `reply` is always the model's
    prose — a clarifying question if resolution failed, otherwise narration
    of `proposals`. `proposals` is populated only when call_repair_solver
    actually ran; the UI should treat a None proposals list as "needs more
    info from the chief", not as "no coverage found" (that's an empty list).
    """

    reply: str
    resolved: bool
    sick_resident_id: int | None = None
    open_shift: OpenShift | None = None
    proposals: list[RankedSwapProposal] | None = None


def handle_callout_message(
    current_schedule: CurrentSchedule,
    free_text: str,
    *,
    today: dt.date | None = None,
    client: OllamaClient | None = None,
) -> CalloutHandlingResult:
    """Development Priority #6: free text -> structured solver call.

    Deliberately does NOT let the model decide when to call call_repair_solver
    with copied-over IDs — tried that with both tools registered at once and
    llama3.1:8b would sometimes fire a premature call_repair_solver in the
    same turn as query_schedule_db, with hallucinated block/rotation/resident
    IDs instead of the real ones (verified live against the local model, not
    a hypothetical). Smaller models are reliable at exactly two things here:
    extracting a name+date from free text, and narrating a result they're
    handed. Everything in between — resolving that to one exact assignment,
    and invoking the solver — happens in plain Python. The solver call
    itself is never something the model can get wrong, hallucinate around,
    or skip.

    today is injected into the prompt so relative dates ("tomorrow",
    "Thursday") resolve against a fact the model is given, not a guess.
    """
    today = today or dt.date.today()
    system_prompt = _PROMPT_PATH.read_text()
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Today's date is {today.isoformat()}. A chief resident just wrote: "
                f'"{free_text}". Call query_schedule_db to find out who is out and '
                "which assignment needs coverage. If you can't tell exactly who or "
                "which single date they mean, don't call the tool — ask a "
                "clarifying question instead."
            ),
        },
    ]

    client = client or OllamaClient(model=DEFAULT_MODEL)

    response = client.chat(messages=messages, tools=[QUERY_SCHEDULE_DB_TOOL], options={"temperature": 0})
    messages.append(response.message.model_dump())

    lookup_calls = [c for c in (response.message.tool_calls or []) if c.function.name == "query_schedule_db"]
    if not lookup_calls:
        return CalloutHandlingResult(
            reply=response.message.content or "Could you clarify who's out and on what date?",
            resolved=False,
        )

    # Trust only the first call — a well-behaved model emits exactly one.
    args = lookup_calls[0].function.arguments
    try:
        matches = query_schedule_db(current_schedule, name=args["name"], date=args["date"])
    except (KeyError, ValueError):
        return CalloutHandlingResult(reply="Could you clarify who's out and on what date?", resolved=False)

    messages.append({"role": "tool", "content": json.dumps(matches), "tool_name": "query_schedule_db"})

    if len(matches) != 1:
        clarify = client.chat(
            messages=messages
            + [
                {
                    "role": "user",
                    "content": (
                        "That lookup returned zero or multiple matches (shown above as "
                        "the tool result). Ask the chief resident a clarifying question "
                        "to narrow it down. Do not guess."
                    ),
                }
            ],
        )
        return CalloutHandlingResult(
            reply=clarify.message.content or "Could you clarify who's out and on what date?",
            resolved=False,
        )

    match = matches[0]
    open_shift = OpenShift(
        block_id=match["block_id"],
        rotation_id=match["rotation_id"],
        role=match["role"],
        date=dt.date.fromisoformat(args["date"]),
        shift_type=DEFAULT_SHIFT_TYPE,
        hours=DEFAULT_SHIFT_HOURS,
    )
    tool_result = call_repair_solver(
        current_schedule,
        block_id=open_shift.block_id,
        rotation_id=open_shift.rotation_id,
        role=open_shift.role,
        date=open_shift.date.isoformat(),
        shift_type=open_shift.shift_type,
        hours=open_shift.hours,
        sick_resident_id=match["resident_id"],
    )

    if not tool_result:
        return CalloutHandlingResult(
            reply=f"No eligible peer was found to cover {match['resident_name']}'s shift.",
            resolved=True,
            sick_resident_id=match["resident_id"],
            open_shift=open_shift,
            proposals=[],
        )

    valid_ids = {c["resident_id"] for c in tool_result}
    narratives: dict[int, str] = {}
    try:
        narration = client.chat(
            messages=messages
            + [
                {
                    "role": "user",
                    "content": (
                        f"{match['resident_name']} is out; here are the ranked coverage "
                        f"candidates: {json.dumps(tool_result)}. Write a one-sentence "
                        "rationale for each, in rank order, as JSON. Use only the "
                        "candidates given — do not add or remove any."
                    ),
                }
            ],
            format=_NARRATIVE_SCHEMA,
        )
        parsed = json.loads(narration.message.content)
        narratives = {n["resident_id"]: n["narrative"] for n in parsed["narratives"] if n["resident_id"] in valid_ids}
    except (json.JSONDecodeError, KeyError, TypeError):
        pass  # fall back to the solver's own reason string below

    proposals = [
        RankedSwapProposal(
            resident_id=c["resident_id"],
            resident_name=c["resident_name"],
            rank=c["rank"],
            projected_window_hours=c["projected_window_hours"],
            narrative=narratives.get(c["resident_id"], c["solver_reason"]),
        )
        for c in tool_result
    ]
    return CalloutHandlingResult(
        reply=f"Found {len(proposals)} option(s) to cover {match['resident_name']}'s shift.",
        resolved=True,
        sick_resident_id=match["resident_id"],
        open_shift=open_shift,
        proposals=proposals,
    )


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


def explain_rule_violation(*args, **kwargs):
    raise NotImplementedError("llm/tools.py: Development Priority #6")
