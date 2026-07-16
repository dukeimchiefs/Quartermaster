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
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from llm.client import DEFAULT_MODEL, OllamaClient
from real_schedule.checks import (
    AssistSwapCheckResult,
    FscReflectionDayCheckResult,
    RotationSwapCheckResult,
    check_assist_swap,
    check_fsc_reflection_day_request,
    check_rotation_swap,
)
from real_schedule.recommend import RankedClinicCandidate, recommend_clinic_coverage
from solver.repair import CurrentSchedule, OpenShift, SwapProposal, repair_schedule

_PROMPT_PATH = Path(__file__).parent / "prompts" / "callout_handler.md"
_CHECK_PROMPT_PATH = Path(__file__).parent / "prompts" / "check_handler.md"

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


# ---------------------------------------------------------------------------
# Free-text handlers for the four real_schedule/ Check tools (pages 4-7).
#
# Same discipline as handle_callout_message above: the model's only jobs are
# (a) extracting identifying fields from free text via one tool call, and
# (b) narrating a result it's handed. Every lookup/validation/decision is
# plain Python against the page's already-loaded real_schedule records —
# never trusted from the model directly. Since there's no DB here, "trusted
# from the model directly" would mean matching by exact string/date equality
# against whatever the model wrote; instead, names are resolved by a
# tokenized, order-independent match (see _match_names) and dates/weeks are
# checked for exact membership in the valid set the page already computed —
# zero or multiple matches is treated as unresolved and re-asked, never
# guessed.
# ---------------------------------------------------------------------------

_NAME_TOKEN_RE = re.compile(r"[^,\s]+")


def _tokenize_name(name: str) -> list[str]:
    return [t.lower() for t in _NAME_TOKEN_RE.findall(name)]


def _match_names(raw: str | None, valid_names: list[str]) -> list[str]:
    """Tokenizes `raw` and each candidate name (split on comma/whitespace,
    lowercased) and requires every query token to prefix-match — in either
    direction — a distinct candidate token. Handles two real problems a
    plain substring check misses: word-order mismatches ("Chris Choi" vs
    the roster's "Choi, Christopher" — same class of issue fixed for
    preceptor names in real_schedule/recommend.py's _same_preceptor), and
    common English diminutives that are literal prefixes of the formal name
    ("Chris" -> "Christopher", "Nick" -> "Nicholas", "Sam" -> "Samuel").
    Not exhaustive — a non-prefix nickname ("Jack" -> "John") still won't
    match — but a genuine non-match still correctly surfaces as zero
    results, never a guess."""
    query_tokens = _tokenize_name(raw or "")
    if not query_tokens:
        return []

    matches = []
    for name in valid_names:
        remaining = _tokenize_name(name)
        if all(_pop_prefix_match(remaining, qt) for qt in query_tokens):
            matches.append(name)
    return matches


def _pop_prefix_match(remaining_tokens: list[str], query_token: str) -> bool:
    """True and removes the first token in `remaining_tokens` that
    prefix-matches `query_token` in either direction; False (no removal) if
    none does. Each candidate token can only satisfy one query token."""
    for i, candidate_token in enumerate(remaining_tokens):
        if candidate_token.startswith(query_token) or query_token.startswith(candidate_token):
            remaining_tokens.pop(i)
            return True
    return False


def _ambiguous_match_reply(field_label: str, raw: str | None, matches: list[str]) -> str:
    """A deterministic, always-grounded message for the zero-or-multiple-
    match case — never phrased by the model, so it can never invent a
    distinguishing detail (specialty, program, etc.) that isn't actually in
    `matches`. See llm/tools.py module notes on the "Chris Choi" bug this
    replaced: the model, given no real candidate data, fabricated a
    plausible-sounding but entirely fictitious clarifying question."""
    if not matches:
        return f"I couldn't find anyone/anything matching {raw!r} for {field_label} — could you double-check it?"
    return f"Found {len(matches)} matches for {field_label} ({raw!r}): {', '.join(matches)}. Which one did you mean?"


def _match_date(raw: str | None, valid_dates) -> dt.date | None:
    if not raw:
        return None
    try:
        parsed = dt.date.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed in valid_dates else None


def _match_week_start(raw: str | None, valid_week_starts) -> dt.date | None:
    if not raw:
        return None
    try:
        parsed = dt.date.fromisoformat(raw)
    except ValueError:
        return None
    week_start = parsed - dt.timedelta(days=parsed.weekday())
    return week_start if week_start in valid_week_starts else None


@dataclass
class CheckHandlingResult:
    """Result of a handle_check_*_message() call. `reply` is either a
    deterministic, Python-authored clarifying message (see
    _ambiguous_match_reply — grounded in the actual matches found, never
    phrased by the model) if resolution failed, or the model's narration of
    `result` once resolution succeeded. `result` is populated only once the
    deterministic checker actually ran; a None result means "needs more
    info from the chief," never "checked and found nothing.\""""

    reply: str
    resolved: bool
    result: object | None = None
    resolved_args: dict | None = None


def _run_check_handler(
    *,
    free_text: str,
    today: dt.date,
    tool_schema: dict,
    resolve_args: Callable[[dict], tuple[dict | None, str | None]],
    run_check: Callable[[dict], object],
    summarize: Callable[[object], str],
    client: OllamaClient | None,
) -> CheckHandlingResult:
    system_prompt = _CHECK_PROMPT_PATH.read_text()
    tool_name = tool_schema["function"]["name"]
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Today's date is {today.isoformat()}. A chief resident just wrote: "
                f'"{free_text}". Call {tool_name} with everything you can confidently '
                "extract. If you can't tell exactly what's meant, don't call the tool "
                "— ask a clarifying question instead."
            ),
        },
    ]
    client = client or OllamaClient(model=DEFAULT_MODEL)

    response = client.chat(messages=messages, tools=[tool_schema], options={"temperature": 0})
    messages.append(response.message.model_dump())

    calls = [c for c in (response.message.tool_calls or []) if c.function.name == tool_name]
    if not calls:
        return CheckHandlingResult(
            reply=response.message.content or "Could you clarify the details of this request?",
            resolved=False,
        )

    resolved_kwargs, error_reply = resolve_args(calls[0].function.arguments)

    if resolved_kwargs is None:
        # error_reply (built via _ambiguous_match_reply) is already a
        # complete, grounded message listing the real matches (or plainly
        # saying there were none) — returned directly, with no further LLM
        # call, so the model has no opportunity to invent a distinguishing
        # detail that isn't actually in the data (see llm/tools.py's module
        # notes on the "Chris Choi" bug this replaced).
        return CheckHandlingResult(reply=error_reply, resolved=False)

    result = run_check(resolved_kwargs)
    summary_text = summarize(result)
    messages.append({"role": "tool", "content": summary_text, "tool_name": f"{tool_name}_result"})

    narration = client.chat(
        messages=messages
        + [
            {
                "role": "user",
                "content": (
                    "Now write a concise (2-3 sentence), chief-resident-facing "
                    "narration of this result. Do not add, remove, or reinterpret any "
                    "finding."
                ),
            }
        ]
    )
    return CheckHandlingResult(
        reply=narration.message.content or summary_text,
        resolved=True,
        result=result,
        resolved_args=resolved_kwargs,
    )


def _summarize_findings(result) -> str:
    return json.dumps(
        {
            "is_clear": result.is_clear,
            "findings": [{"rule": f.rule, "severity": f.severity, "message": f.message} for f in result.findings],
        }
    )


RESOLVE_ROTATION_SWAP_TOOL = {
    "type": "function",
    "function": {
        "name": "resolve_rotation_swap_request",
        "description": "Resolve a proposed mutual rotation swap into the two residents and week range it refers to.",
        "parameters": {
            "type": "object",
            "properties": {
                "resident_1": {"type": "string", "description": "First resident's name, as mentioned by the chief."},
                "resident_2": {"type": "string", "description": "Second resident's name, as mentioned by the chief."},
                "week_start_first": {"type": "string", "description": "ISO date (YYYY-MM-DD) of the first week of the swap."},
                "week_start_last": {"type": "string", "description": "ISO date (YYYY-MM-DD) of the last week of the swap."},
            },
            "required": ["resident_1", "resident_2", "week_start_first", "week_start_last"],
        },
    },
}


def handle_check_rotation_swap_message(
    master_schedule,
    master_assist,
    free_text: str,
    *,
    today: dt.date | None = None,
    client: OllamaClient | None = None,
) -> CheckHandlingResult:
    today = today or dt.date.today()
    resident_names = sorted({r.resident_name for r in master_schedule})
    week_starts = sorted({r.week_start for r in master_schedule})

    def resolve_args(args: dict) -> tuple[dict | None, str | None]:
        matches_1 = _match_names(args.get("resident_1"), resident_names)
        matches_2 = _match_names(args.get("resident_2"), resident_names)
        if len(matches_1) != 1:
            return None, _ambiguous_match_reply("resident #1", args.get("resident_1"), matches_1)
        if len(matches_2) != 1:
            return None, _ambiguous_match_reply("resident #2", args.get("resident_2"), matches_2)
        week_first = _match_week_start(args.get("week_start_first"), week_starts)
        week_last = _match_week_start(args.get("week_start_last"), week_starts)
        if week_first is None or week_last is None or week_first > week_last:
            return None, "Couldn't resolve a valid week range from the Master Schedule."
        return (
            {
                "resident_1": matches_1[0],
                "resident_2": matches_2[0],
                "week_starts": [w for w in week_starts if week_first <= w <= week_last],
            },
            None,
        )

    def run_check(kwargs: dict) -> RotationSwapCheckResult:
        return check_rotation_swap(
            kwargs["resident_1"], kwargs["resident_2"], kwargs["week_starts"],
            master_schedule=master_schedule, master_assist=master_assist,
        )

    return _run_check_handler(
        free_text=free_text, today=today, tool_schema=RESOLVE_ROTATION_SWAP_TOOL,
        resolve_args=resolve_args, run_check=run_check, summarize=_summarize_findings, client=client,
    )


RESOLVE_ASSIST_SWAP_TOOL = {
    "type": "function",
    "function": {
        "name": "resolve_assist_swap_request",
        "description": "Resolve a proposed jeopardy/assist week swap into the two residents and the two weeks involved.",
        "parameters": {
            "type": "object",
            "properties": {
                "resident_1": {"type": "string", "description": "Resident currently on jeopardy/assist, as mentioned by the chief."},
                "resident_2": {"type": "string", "description": "Resident proposed to cover, as mentioned by the chief."},
                "week_covered": {"type": "string", "description": "ISO date (YYYY-MM-DD) of the week resident #2 would cover."},
                "week_new": {"type": "string", "description": "ISO date (YYYY-MM-DD) of resident #1's new week instead."},
            },
            "required": ["resident_1", "resident_2", "week_covered", "week_new"],
        },
    },
}


def handle_check_assist_swap_message(
    master_assist,
    weekly_assist,
    master_schedule,
    free_text: str,
    *,
    today: dt.date | None = None,
    client: OllamaClient | None = None,
) -> CheckHandlingResult:
    today = today or dt.date.today()
    resident_names = sorted({d.resident_name for d in master_assist} | {e.resident_name for e in weekly_assist})
    week_starts = sorted({e.week_start for e in weekly_assist})

    def resolve_args(args: dict) -> tuple[dict | None, str | None]:
        matches_1 = _match_names(args.get("resident_1"), resident_names)
        matches_2 = _match_names(args.get("resident_2"), resident_names)
        if len(matches_1) != 1:
            return None, _ambiguous_match_reply("resident #1", args.get("resident_1"), matches_1)
        if len(matches_2) != 1:
            return None, _ambiguous_match_reply("resident #2", args.get("resident_2"), matches_2)
        week_covered = _match_week_start(args.get("week_covered"), week_starts)
        week_new = _match_week_start(args.get("week_new"), week_starts)
        if week_covered is None or week_new is None:
            return None, "Couldn't resolve both weeks against the real assist-list schedule."
        return {"resident_1": matches_1[0], "resident_2": matches_2[0], "week_covered": week_covered, "week_new": week_new}, None

    def run_check(kwargs: dict) -> AssistSwapCheckResult:
        return check_assist_swap(
            kwargs["resident_1"], kwargs["resident_2"], kwargs["week_covered"], kwargs["week_new"],
            master_assist=master_assist, weekly_assist=weekly_assist, master_schedule=master_schedule,
        )

    return _run_check_handler(
        free_text=free_text, today=today, tool_schema=RESOLVE_ASSIST_SWAP_TOOL,
        resolve_args=resolve_args, run_check=run_check, summarize=_summarize_findings, client=client,
    )


RESOLVE_FSC_REFLECTION_TOOL = {
    "type": "function",
    "function": {
        "name": "resolve_fsc_reflection_request",
        "description": "Resolve a proposed FSC/Reflection day request into the resident, specific date, and portion.",
        "parameters": {
            "type": "object",
            "properties": {
                "resident": {"type": "string", "description": "Resident's name, as mentioned by the chief."},
                "date": {"type": "string", "description": "ISO date (YYYY-MM-DD) of the requested day, within the currently-selected ambulatory week."},
                "portion": {"type": "string", "enum": ["AM", "PM", "FULL"], "description": "Half-day AM, half-day PM, or a full day."},
            },
            "required": ["resident", "date", "portion"],
        },
    },
}


def handle_check_fsc_reflection_message(
    master_schedule,
    master_assist,
    ambulatory_week,
    fsc_balances,
    free_text: str,
    *,
    today: dt.date | None = None,
    client: OllamaClient | None = None,
) -> CheckHandlingResult:
    """Resolves within the currently-selected (already-loaded) ambulatory
    week — same scope as the structured form's own week dropdown; picking a
    different week still means picking it from that dropdown first."""
    today = today or dt.date.today()
    resident_names = sorted({r.resident_name for r in master_schedule})
    candidate_dates = sorted({d for row in ambulatory_week for (d, _half) in row.day_parts})

    def resolve_args(args: dict) -> tuple[dict | None, str | None]:
        matches = _match_names(args.get("resident"), resident_names)
        if len(matches) != 1:
            return None, _ambiguous_match_reply("resident", args.get("resident"), matches)
        date_ = _match_date(args.get("date"), candidate_dates)
        if date_ is None:
            return None, "Couldn't resolve that date within the currently-selected ambulatory week."
        portion = args.get("portion")
        if portion not in ("AM", "PM", "FULL"):
            return None, "Couldn't tell if this is a half-day (AM/PM) or full-day request."
        return {"resident": matches[0], "date": date_, "portion": portion}, None

    def run_check(kwargs: dict) -> FscReflectionDayCheckResult:
        return check_fsc_reflection_day_request(
            kwargs["resident"], kwargs["date"], kwargs["portion"],
            master_schedule=master_schedule, master_assist=master_assist,
            ambulatory_week=ambulatory_week, fsc_balances=fsc_balances,
        )

    return _run_check_handler(
        free_text=free_text, today=today, tool_schema=RESOLVE_FSC_REFLECTION_TOOL,
        resolve_args=resolve_args, run_check=run_check, summarize=_summarize_findings, client=client,
    )


RESOLVE_CLINIC_COVERAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "resolve_clinic_coverage_request",
        "description": "Resolve a preceptor call-out into the preceptor, date, and half-day, within the currently-selected ambulatory week.",
        "parameters": {
            "type": "object",
            "properties": {
                "called_out_preceptor": {"type": "string", "description": "Name of the preceptor who called out, as mentioned by the chief."},
                "date": {"type": "string", "description": "ISO date (YYYY-MM-DD), within the currently-selected ambulatory week."},
                "half_day": {"type": "string", "enum": ["AM", "PM"]},
            },
            "required": ["called_out_preceptor", "date", "half_day"],
        },
    },
}


def handle_check_clinic_coverage_message(
    ambulatory_week,
    available_clinics,
    free_text: str,
    *,
    today: dt.date | None = None,
    client: OllamaClient | None = None,
) -> CheckHandlingResult:
    """Resolves called_out_preceptor/date/half_day only — candidate
    preceptor and location are no longer manually entered; this feeds
    straight into recommend_clinic_coverage() instead of a direct
    check_clinic_reassignment() call. `result` is a
    list[RankedClinicCandidate] (possibly empty) rather than a single
    *CheckResult."""
    from real_schedule.common import is_preceptor_cell

    today = today or dt.date.today()
    preceptor_names = sorted(
        {
            is_preceptor_cell(cell)[0]
            for row in ambulatory_week
            for cell in row.day_parts.values()
            if is_preceptor_cell(cell) is not None
        }
    )
    valid_day_parts = {(d, h) for row in ambulatory_week for (d, h) in row.day_parts}

    def resolve_args(args: dict) -> tuple[dict | None, str | None]:
        matches = _match_names(args.get("called_out_preceptor"), preceptor_names)
        if len(matches) != 1:
            return None, _ambiguous_match_reply("the called-out preceptor", args.get("called_out_preceptor"), matches)
        try:
            date_ = dt.date.fromisoformat(args.get("date", ""))
        except ValueError:
            return None, "Couldn't resolve that date within the currently-selected ambulatory week."
        half_day = args.get("half_day")
        if half_day not in ("AM", "PM") or (date_, half_day) not in valid_day_parts:
            return None, "Couldn't resolve that date/half-day within the currently-selected ambulatory week."
        return {"called_out_preceptor": matches[0], "date": date_, "half_day": half_day}, None

    def run_check(kwargs: dict) -> list[RankedClinicCandidate]:
        return recommend_clinic_coverage(
            kwargs["called_out_preceptor"], kwargs["date"], kwargs["half_day"],
            ambulatory_week=ambulatory_week, available_clinics=available_clinics,
        )

    def summarize(candidates: list[RankedClinicCandidate]) -> str:
        return json.dumps(
            [
                {
                    "rank": c.rank, "preceptor_name": c.preceptor_name, "location": c.location,
                    "is_clear": c.is_clear, "same_site_group": c.same_site_group,
                    "findings": [{"rule": f.rule, "severity": f.severity, "message": f.message} for f in c.findings],
                }
                for c in candidates
            ]
        )

    return _run_check_handler(
        free_text=free_text, today=today, tool_schema=RESOLVE_CLINIC_COVERAGE_TOOL,
        resolve_args=resolve_args, run_check=run_check, summarize=summarize, client=client,
    )
