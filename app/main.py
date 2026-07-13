"""Streamlit entrypoint — local-only, no external CDN.

Run with `streamlit run app/main.py`. Pages in app/pages/ are auto-discovered
by Streamlit's classic multipage navigation; each page gates itself with
app/auth.py's require_chief_auth() since a user can jump straight to any
page script from the sidebar.

Development Priority #4/#9/#10 (CLAUDE.md) — pages land incrementally as
their backing solver/LLM pieces are built.
"""

from __future__ import annotations

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
        "- **Build Schedule** — not yet implemented (Development Priority #9)\n"
        "- **Review Changes** — not yet implemented (Development Priority #10)"
    )


if __name__ == "__main__":
    main()
