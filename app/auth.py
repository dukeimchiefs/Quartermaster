"""App-level passphrase gate.

Defense-in-depth on top of CLAUDE.md's required OS-level auth + auto-lock:
this tool is chief-resident-only, chiefs rotate yearly, and the workstation
may sit in a shared workroom, so every page is gated behind a single local
passphrase rather than relying solely on the OS login.

The passphrase lives in .streamlit/secrets.toml (gitignored — copy
.streamlit/secrets.toml.example and set a real value). Never hardcode it
here.

The passphrase is shared by design (one chief at a time, rotates yearly) so
it carries no identity on its own — but CLAUDE.md's audit log requires an
"actor" on every entry, and "the shared passphrase" isn't one. So the login
form also asks for a name, held only in session state, purely to attribute
audit_log rows (see audit/log.py) to whoever is actually at the keyboard.
It is not a credential and proves nothing by itself.
"""

from __future__ import annotations

import streamlit as st

SESSION_KEY = "chief_authenticated"
ACTOR_SESSION_KEY = "chief_actor_name"
DEFAULT_ACTOR = "unknown_chief"


def _configured_passphrase() -> str | None:
    try:
        return st.secrets.get("chief_passphrase")
    except Exception:
        return None


def get_actor() -> str:
    """The name entered at login, for audit_log attribution. Falls back to
    a placeholder rather than raising, since audit logging must never be
    the thing that breaks a page."""
    return st.session_state.get(ACTOR_SESSION_KEY) or DEFAULT_ACTOR


def require_chief_auth() -> None:
    """Block rendering of the calling page until the correct passphrase is
    entered. Call this as the first line of every page script — Streamlit's
    classic multipage navigation lets a user jump straight to any page
    script, so main.py alone can't gate the others."""
    if st.session_state.get(SESSION_KEY):
        return

    st.title("Resident Scheduling Assistant")
    st.caption("Chief-resident use only.")

    expected = _configured_passphrase()
    if not expected:
        st.error(
            "No passphrase configured. Copy .streamlit/secrets.toml.example to "
            ".streamlit/secrets.toml and set chief_passphrase."
        )
        st.stop()

    with st.form("chief_auth_form"):
        name = st.text_input("Your name (for the audit log)")
        passphrase = st.text_input("Passphrase", type="password")
        submitted = st.form_submit_button("Enter")

    if submitted:
        if passphrase == expected:
            st.session_state[SESSION_KEY] = True
            st.session_state[ACTOR_SESSION_KEY] = name.strip() or DEFAULT_ACTOR
            st.rerun()
        else:
            st.error("Incorrect passphrase.")

    st.stop()
