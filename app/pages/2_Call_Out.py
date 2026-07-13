"""Page 2 — Call Out.

Development Priority #4 (CLAUDE.md): structured form + result display, ahead
of the LLM-driven free-text path (Priority #6). Reads the live DB, calls
solver/repair.py, and displays ranked replacement proposals. This page does
not commit anything — no assignment or call_history row is written here.
Picking a candidate stages it in st.session_state["pending_swap"] for
Page 3 (Review Changes) to diff and actually commit (Priority #10); only
one pending swap is held at a time, choosing a new one replaces it.

It writes to audit_log (Priority #7) every time a search actually produces
(or fails to produce) a proposed swap — CLAUDE.md requires every proposed
*or* committed change to be logged, not just commits. A free-text message
that only led to a clarifying question isn't a proposal yet, so those
aren't logged here.

Search results are stored in session_state rather than rendered directly
inside the `if st.button(...)` block: Streamlit reruns the whole script on
every widget interaction, and a "choose this option" button nested inside
that block would make the outer condition go false on the very rerun that's
supposed to handle its click, wiping the results before they can be acted
on. Keeping computed results in session_state and rendering them in a
separate, unconditional section avoids that.
"""

from __future__ import annotations

import datetime as dt
import json

import streamlit as st

from app.auth import get_actor, require_chief_auth
from audit.log import record as audit_record
from db.models import Assignment, Block, CallHistory, Resident, Rotation, TimeOff, get_engine, get_session
from llm.tools import handle_callout_message, recommend_swaps
from solver.repair import CurrentSchedule, OpenShift, repair_schedule

require_chief_auth()

st.title("Call Out")
st.caption("Find replacement coverage for a resident who's out.")


@st.cache_resource
def _engine():
    return get_engine()


def _open_shift_dict(open_shift: OpenShift | None) -> dict | None:
    if open_shift is None:
        return None
    return {
        "block_id": open_shift.block_id,
        "rotation_id": open_shift.rotation_id,
        "role": open_shift.role,
        "date": open_shift.date.isoformat(),
        "shift_type": open_shift.shift_type,
        "hours": open_shift.hours,
    }


def _load_schedule() -> CurrentSchedule:
    with get_session(_engine()) as session:
        return CurrentSchedule(
            assignments=session.query(Assignment).all(),
            residents=session.query(Resident).all(),
            rotations=session.query(Rotation).all(),
            blocks=session.query(Block).all(),
            time_off=session.query(TimeOff).all(),
            call_history=session.query(CallHistory).all(),
        )


schedule = _load_schedule()

if not schedule.residents or not schedule.assignments:
    st.warning("No schedule data found. Run `python -m db.seed` to load the toy dataset.")
    st.stop()

rotations_by_id = {r.id: r for r in schedule.rotations}
blocks_by_id = {b.id: b for b in schedule.blocks}
residents_by_id = {r.id: r for r in schedule.residents}


def _stage_pending_swap(*, sick_resident_id, open_shift_dict, candidate, source: str) -> None:
    st.session_state["pending_swap"] = {
        "source": source,
        "sick_resident_id": sick_resident_id,
        "sick_resident_name": residents_by_id[sick_resident_id].name,
        "chosen_resident_id": candidate["resident_id"],
        "chosen_resident_name": candidate["resident_name"],
        "open_shift": open_shift_dict,
        "reason": candidate["reason"],
    }
    st.success(f"Staged {candidate['resident_name']} to cover this shift — go to Review Changes to approve.")


sick_resident = st.selectbox(
    "Who's out?",
    options=schedule.residents,
    format_func=lambda r: f"{r.name} (PGY-{r.pgy})",
)

their_assignments = [a for a in schedule.assignments if a.resident_id == sick_resident.id]
if not their_assignments:
    st.warning(f"{sick_resident.name} has no block assignment in the current schedule.")
    st.stop()

assignment = st.selectbox(
    "Which assignment needs coverage?",
    options=their_assignments,
    format_func=lambda a: (
        f"Block {blocks_by_id[a.block_id].block_number} — "
        f"{rotations_by_id[a.rotation_id].name} ({a.role})"
    ),
)
block = blocks_by_id[assignment.block_id]
rotation = rotations_by_id[assignment.rotation_id]

col1, col2, col3 = st.columns(3)
with col1:
    shift_date = st.date_input(
        "Shift date",
        value=block.start_date,
        min_value=block.start_date,
        max_value=block.end_date,
    )
with col2:
    shift_type = st.text_input("Shift type", value="night_call")
with col3:
    hours = st.number_input("Hours", min_value=1.0, max_value=30.0, value=14.0, step=1.0)

ask_assistant = st.checkbox(
    "Ask the local assistant to explain each option",
    help=(
        "Runs the ranked candidates through the local Ollama model for a "
        "plain-language rationale (Development Priority #5). The solver's "
        "ranking and candidate list never change — the assistant only adds "
        "narration. Requires `ollama serve` running locally; falls back to "
        "the solver's own reason if it isn't reachable."
    ),
)

if st.button("Find coverage", type="primary"):
    open_shift = OpenShift(
        block_id=block.id,
        rotation_id=rotation.id,
        role=assignment.role,
        date=shift_date if isinstance(shift_date, dt.date) else shift_date[0],
        shift_type=shift_type,
        hours=hours,
    )
    proposals = repair_schedule(schedule, open_shift, sick_resident=sick_resident.id)

    audit_record(
        actor=get_actor(),
        action="propose_swap",
        reason=(
            f"call-out: {sick_resident.name} out for {rotation.name} ({assignment.role}) "
            f"block {block.block_number}, {shift_type} on {open_shift.date.isoformat()}"
        ),
        details=json.dumps(
            {
                "sick_resident_id": sick_resident.id,
                "open_shift": _open_shift_dict(open_shift),
                "candidates": [
                    {"resident_id": p.resident_id, "rank": p.rank, "projected_window_hours": p.projected_window_hours}
                    for p in proposals
                ],
            }
        ),
    )

    narratives_by_id: dict[int, str] = {}
    if proposals and ask_assistant:
        try:
            with st.spinner("Asking the local assistant..."):
                ranked = recommend_swaps(schedule, open_shift, sick_resident=sick_resident.id, candidates=proposals)
            narratives_by_id = {r.resident_id: r.narrative for r in ranked}
        except Exception as exc:  # Ollama not running, model not pulled, etc.
            st.warning(f"Local assistant unavailable, showing solver's own reason instead ({exc}).")

    st.session_state["structured_search_result"] = {
        "sick_resident_id": sick_resident.id,
        "rotation_name": rotation.name,
        "role": assignment.role,
        "block_number": block.block_number,
        "open_shift": _open_shift_dict(open_shift),
        "candidates": [
            {
                "resident_id": p.resident_id,
                "resident_name": residents_by_id[p.resident_id].name,
                "rank": p.rank,
                "projected_window_hours": p.projected_window_hours,
                "reason": narratives_by_id.get(p.resident_id, p.reason),
            }
            for p in proposals
        ],
    }

structured_result = st.session_state.get("structured_search_result")
if structured_result:
    candidates = structured_result["candidates"]
    if not candidates:
        st.error(
            f"No eligible peer found to cover {structured_result['rotation_name']} "
            f"({structured_result['role']}) in block {structured_result['block_number']}."
        )
    else:
        st.success(f"Found {len(candidates)} candidate(s).")
        for candidate in candidates:
            with st.container(border=True):
                candidate_resident = residents_by_id[candidate["resident_id"]]
                st.markdown(f"**#{candidate['rank']} — {candidate_resident.name}** (PGY-{candidate_resident.pgy})")
                st.caption(candidate["reason"])
                st.metric("Projected rolling-window hours", f"{candidate['projected_window_hours']:.1f}h")
                if st.button(f"Choose {candidate_resident.name}", key=f"choose_structured_{candidate['resident_id']}"):
                    _stage_pending_swap(
                        sick_resident_id=structured_result["sick_resident_id"],
                        open_shift_dict=structured_result["open_shift"],
                        candidate=candidate,
                        source="structured",
                    )

st.divider()
st.subheader("Or describe it in your own words")
st.caption(
    "Development Priority #6 (CLAUDE.md). Parses free text into who/when via "
    "the local assistant, then always runs the same solver as the form above "
    "— it never invents a candidate itself. Defaults to a 14h night_call "
    "shift unless you say otherwise."
)
free_text = st.text_area(
    "What happened?",
    placeholder='e.g. "Alice is out tomorrow, possibly Thursday too, with the flu"',
)
if st.button("Parse & find coverage"):
    if not free_text.strip():
        st.warning("Enter a description first.")
    else:
        try:
            with st.spinner("Asking the local assistant..."):
                result = handle_callout_message(schedule, free_text, today=dt.date.today())
        except Exception as exc:  # Ollama not running, model not pulled, etc.
            st.error(f"Local assistant unavailable ({exc}). Use the structured form above instead.")
            st.session_state.pop("free_text_search_result", None)
        else:
            if not result.resolved:
                st.info(result.reply)
                st.session_state.pop("free_text_search_result", None)
            else:
                audit_record(
                    actor=get_actor(),
                    action="propose_swap",
                    reason=f'call-out (free text): "{free_text.strip()}"',
                    details=json.dumps(
                        {
                            "free_text": free_text.strip(),
                            "sick_resident_id": result.sick_resident_id,
                            "open_shift": _open_shift_dict(result.open_shift),
                            "candidates": [
                                {
                                    "resident_id": p.resident_id,
                                    "rank": p.rank,
                                    "projected_window_hours": p.projected_window_hours,
                                }
                                for p in result.proposals
                            ],
                        }
                    ),
                )
                st.session_state["free_text_search_result"] = {
                    "reply": result.reply,
                    "sick_resident_id": result.sick_resident_id,
                    "open_shift": _open_shift_dict(result.open_shift),
                    "candidates": [
                        {
                            "resident_id": p.resident_id,
                            "resident_name": p.resident_name,
                            "rank": p.rank,
                            "projected_window_hours": p.projected_window_hours,
                            "reason": p.narrative,
                        }
                        for p in result.proposals
                    ],
                }

free_text_result = st.session_state.get("free_text_search_result")
if free_text_result:
    if not free_text_result["candidates"]:
        st.error(free_text_result["reply"])
    else:
        st.success(free_text_result["reply"])
        for candidate in free_text_result["candidates"]:
            with st.container(border=True):
                st.markdown(f"**#{candidate['rank']} — {candidate['resident_name']}**")
                st.caption(candidate["reason"])
                st.metric("Projected rolling-window hours", f"{candidate['projected_window_hours']:.1f}h")
                if st.button(f"Choose {candidate['resident_name']}", key=f"choose_freetext_{candidate['resident_id']}"):
                    _stage_pending_swap(
                        sick_resident_id=free_text_result["sick_resident_id"],
                        open_shift_dict=free_text_result["open_shift"],
                        candidate=candidate,
                        source="free_text",
                    )
