"""Streamlit entrypoint — local-only, no external CDN.

Run with `streamlit run app/main.py`. Pages in app/pages/ are auto-discovered
by Streamlit's classic multipage navigation; each page gates itself with
app/auth.py's require_chief_auth() since a user can jump straight to any
page script from the sidebar.

Development Priority #4/#9/#10 (CLAUDE.md) — pages land incrementally as
their backing solver/LLM pieces are built.

Streamlit's own sys.path handling (bootstrap._fix_sys_path) inserts this
script's directory (app/) onto sys.path, not the project root — so
`from app.auth import ...` (and every page's `from db.models import ...`,
`from solver... import ...`, etc.) can't resolve `app`/`db`/`solver`/`llm`
as top-level packages without the root on sys.path too. Confirmed live:
without this fix, both this file and every page under app/pages/ fail with
`ModuleNotFoundError: No module named 'app'` the moment they're actually
rendered by `streamlit run` — a plain `curl` or direct `python -c "exec(...)"`
check (with cwd already at the project root) won't catch this, since
neither goes through Streamlit's own script-loading path. The fix only
needs to run once, here, since Streamlit executes every page rerun in this
same long-lived process — the fixed sys.path persists across page
navigation for the life of the server.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st

from app.auth import require_chief_auth


def main() -> None:
    st.set_page_config(page_title="Resident Scheduling Assistant", page_icon="🗓️")
    require_chief_auth()

    st.title("Resident Scheduling Assistant")
    st.caption("Prototype — pending Duke AI/data governance and GME sign-off.")
    st.markdown(
        "Use the sidebar to navigate:\n"
        "- **Call Out** — find replacement coverage for a sick resident\n"
        "- **Build Schedule** — build a full block schedule for a year from scratch\n"
        "- **Review Changes** — approve or reject staged proposals from the other two pages\n"
        "- **Check Assist Swap** — verify a proposed jeopardy/assist week-swap against the real, live schedules\n"
        "- **Check Clinic Coverage** — verify a proposed resident reassignment after an ambulatory preceptor calls out\n"
        "- **Check FSC/Reflection Day** — verify a resident can take a proposed FSC/Reflection day or half-day away from clinic\n"
        "- **Check Rotation Swap** — verify a proposed mutual rotation swap between two residents\n"
        "- **Check Day Off Alignment** — verify a proposed SAC (specific day-off) request against the inpatient schedule"
    )


if __name__ == "__main__":
    main()
