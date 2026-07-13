"""Page 3 — Review Changes.

Development Priority #10 (CLAUDE.md): the diff viewer, and the only place
in this app that writes to `assignments`, `call_history`, or `swaps` — Page
1 and Page 2 only ever propose and log to audit_log. Reads two staging
slots from st.session_state (set by those pages):

- "pending_swap" (from Page 2): a chosen replacement for one open shift.
  Approving inserts one call_history row for the replacement and one swaps
  row recording what happened; it does not touch assignments — the sick
  resident's block-level rotation assignment doesn't change, only who
  covers that one shift.
- "pending_full_schedule" (from Page 1): a full year's proposed rotation
  assignments. Approving upserts each (resident, block) assignment in
  place — updating rotation_id/role on the existing row if one exists
  (matched via the UNIQUE(resident_id, block_id) constraint) rather than
  deleting and recreating, so any swaps row that references an existing
  assignment's id by foreign key stays valid. An assignment that existed
  before but has no counterpart in the new schedule (e.g. the resident is
  now on approved leave that block) is deleted; if a swaps row references
  it, that delete will fail its foreign-key constraint and the whole
  commit is rolled back with a clear error rather than partially applied.

Every commit writes to audit_log (action=commit_swap / commit_full_schedule)
with actor and reason, per CLAUDE.md's audit requirement.
"""

from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import sqlalchemy.exc
import streamlit as st

from app.auth import get_actor, require_chief_auth
from audit.log import record as audit_record
from db.models import Assignment, Block, CallHistory, Resident, Rotation, Swap, get_engine, get_session

require_chief_auth()

st.title("Review Changes")
st.caption("Approve or reject staged proposals from Call Out and Build Schedule.")


@st.cache_resource
def _engine():
    return get_engine()


pending_swap = st.session_state.get("pending_swap")
pending_full_schedule = st.session_state.get("pending_full_schedule")

if not pending_swap and not pending_full_schedule:
    st.info(
        "Nothing staged for review right now. Go to **Call Out** to choose a coverage option, "
        "or **Build Schedule** to build and stage a full schedule."
    )
    st.stop()


if pending_swap:
    st.subheader("Pending call-out swap")

    with get_session(_engine()) as session:
        sick = session.get(Resident, pending_swap["sick_resident_id"])
        chosen = session.get(Resident, pending_swap["chosen_resident_id"])
        rotation = session.get(Rotation, pending_swap["open_shift"]["rotation_id"])
        block = session.get(Block, pending_swap["open_shift"]["block_id"])

    open_shift = pending_swap["open_shift"]
    with st.container(border=True):
        st.markdown(f"**{sick.name}** is out for a {open_shift['shift_type']} shift on {open_shift['date']}")
        st.markdown(
            f"Rotation: {rotation.name} ({open_shift['role']}), block {block.block_number}, {open_shift['hours']}h"
        )
        st.markdown(f"Proposed coverage: **{chosen.name}**")
        st.caption(pending_swap["reason"])

    col1, col2 = st.columns(2)
    with col1:
        approve_swap = st.button("Approve swap", type="primary", key="approve_swap")
    with col2:
        reject_swap = st.button("Reject swap", key="reject_swap")

    if approve_swap:
        with get_session(_engine()) as session:
            original_assignment = (
                session.query(Assignment).filter_by(resident_id=sick.id, block_id=block.id).one_or_none()
            )
            if original_assignment is None:
                st.error(f"{sick.name} has no assignment for block {block.block_number} anymore — can't commit.")
            else:
                session.add(
                    CallHistory(
                        resident_id=chosen.id,
                        date=dt.date.fromisoformat(open_shift["date"]),
                        shift_type=open_shift["shift_type"],
                        hours=open_shift["hours"],
                    )
                )
                session.add(
                    Swap(
                        original_assignment_id=original_assignment.id,
                        new_assignment_id=None,
                        reason=pending_swap["reason"],
                        approved_by=get_actor(),
                    )
                )
                session.commit()

                audit_record(
                    actor=get_actor(),
                    action="commit_swap",
                    reason=f"{chosen.name} covers {sick.name}'s {open_shift['shift_type']} on {open_shift['date']}",
                    details=json.dumps(pending_swap),
                )
                st.session_state.pop("pending_swap", None)
                st.success("Committed.")
                st.rerun()

    if reject_swap:
        audit_record(
            actor=get_actor(),
            action="reject_swap",
            reason=f"rejected proposed coverage by {chosen.name} for {sick.name}",
            details=json.dumps(pending_swap),
        )
        st.session_state.pop("pending_swap", None)
        st.info("Rejected.")
        st.rerun()


if pending_full_schedule:
    st.subheader("Pending full schedule")

    year = pending_full_schedule["year"]
    proposed = {
        (a["resident_id"], a["block_id"]): (a["rotation_id"], a["role"]) for a in pending_full_schedule["assignments"]
    }

    with get_session(_engine()) as session:
        residents = {r.id: r for r in session.query(Resident).all()}
        rotations = {r.id: r for r in session.query(Rotation).all()}
        blocks = {b.id: b for b in session.query(Block).all() if b.year == year}
        current_assignments = session.query(Assignment).filter(Assignment.block_id.in_(blocks.keys())).all()
        current = {(a.resident_id, a.block_id): (a.rotation_id, a.role) for a in current_assignments}

    all_keys = set(current) | set(proposed)
    rows = []
    for resident_id, block_id in all_keys:
        current_val = current.get((resident_id, block_id))
        proposed_val = proposed.get((resident_id, block_id))
        if current_val == proposed_val:
            status = "unchanged"
        elif current_val is None:
            status = "added"
        elif proposed_val is None:
            status = "removed"
        else:
            status = "changed"
        rows.append(
            {
                "Resident": residents[resident_id].name,
                "Block": blocks[block_id].block_number,
                "Current": f"{rotations[current_val[0]].name} ({current_val[1]})" if current_val else "—",
                "Proposed": f"{rotations[proposed_val[0]].name} ({proposed_val[1]})" if proposed_val else "—",
                "Status": status,
            }
        )

    diff_df = pd.DataFrame(rows).sort_values(["Block", "Resident"])
    changed_df = diff_df[diff_df["Status"] != "unchanged"]

    st.caption(f"{len(changed_df)} of {len(diff_df)} resident-block slots change ({year}).")
    show_all = st.checkbox("Show unchanged slots too")
    st.dataframe(diff_df if show_all else changed_df, use_container_width=True, hide_index=True)

    col1, col2 = st.columns(2)
    with col1:
        approve_schedule = st.button("Approve & commit schedule", type="primary", key="approve_schedule")
    with col2:
        reject_schedule = st.button("Reject schedule", key="reject_schedule")

    if approve_schedule:
        with get_session(_engine()) as session:
            try:
                existing_by_key = {
                    (a.resident_id, a.block_id): a
                    for a in session.query(Assignment).filter(Assignment.block_id.in_(blocks.keys())).all()
                }
                for (resident_id, block_id), existing in existing_by_key.items():
                    if (resident_id, block_id) not in proposed:
                        session.delete(existing)

                for (resident_id, block_id), (rotation_id, role) in proposed.items():
                    existing = existing_by_key.get((resident_id, block_id))
                    if existing is not None:
                        existing.rotation_id = rotation_id
                        existing.role = role
                    else:
                        session.add(
                            Assignment(resident_id=resident_id, block_id=block_id, rotation_id=rotation_id, role=role)
                        )
                session.commit()
            except sqlalchemy.exc.IntegrityError as exc:
                session.rollback()
                st.error(
                    "Commit failed and was rolled back — likely because a previously committed "
                    f"swap references an assignment this schedule would remove. Details: {exc}"
                )
            else:
                audit_record(
                    actor=get_actor(),
                    action="commit_full_schedule",
                    reason=f"committed full schedule for year {year}",
                    details=json.dumps({"year": year, "assignment_count": len(proposed)}),
                )
                st.session_state.pop("pending_full_schedule", None)
                st.success("Committed.")
                st.rerun()

    if reject_schedule:
        audit_record(
            actor=get_actor(),
            action="reject_full_schedule",
            reason=f"rejected proposed full schedule for year {year}",
            details=json.dumps({"year": year, "assignment_count": len(proposed)}),
        )
        st.session_state.pop("pending_full_schedule", None)
        st.info("Rejected.")
        st.rerun()
