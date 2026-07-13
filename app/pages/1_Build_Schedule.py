"""Page 1 — Build Schedule.

Development Priority #9 (CLAUDE.md). Depends on solver/full_schedule.py and
llm/prompts/schedule_builder.md, neither of which is implemented yet.
"""

from __future__ import annotations

import streamlit as st

from app.auth import require_chief_auth

require_chief_auth()

st.title("Build Schedule")
st.info("Not implemented yet — Development Priority #9 (CLAUDE.md).")
