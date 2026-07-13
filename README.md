# Resident Scheduling Assistant

A fully local (offline / on-prem) tool to help a chief resident build the IM
residency block schedule, handle call-outs and swaps, and revise the schedule
mid-cycle. See `CLAUDE.md` for full architecture and design decisions.

## Status

**Prototype.** Governance sign-off from Duke AI/data governance and the GME
office is pending — this must not influence official schedules until that
review is complete.

Implemented (Development Priorities #1–#7 in `CLAUDE.md`): DB schema/seed
data, `rules.py`, the call-out repair solver, the Call-Out Streamlit page
(structured form + free-text parsing), Ollama tool-calling integration, and
audit log wiring. `full_schedule.py`, Page 1 (Build Schedule), Page 3
(Review Changes), and `warm_start.py` (Priorities #8–#11) remain stubbed
with `NotImplementedError`.

## PII / data handling

`Resident_Schedules/` (if present locally) is real, live roster data synced
from Duke OneDrive and contains PII. It is git-ignored and **must never be
read from or written to by this codebase**, in this phase or any later one.
All seed and test data in this repo is fictional.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Seed the toy database

```bash
python -m db.seed
```

## Run tests

```bash
pytest
```

## Run the local LLM (Ollama)

The Call-Out page's "ask the assistant" option and free-text parsing need a
local Ollama server with `llama3.1:8b` (or another local-weights model)
pulled:

```bash
brew install ollama
ollama pull llama3.1:8b
./scripts/run_ollama.sh   # NOT `ollama serve` directly — see below
```

**Always start the server via `scripts/run_ollama.sh`, never `ollama serve`
directly.** Ollama ships a "cloud" feature (remote inference + web search
proxied through ollama.com) that's enabled unless the server process has
`OLLAMA_NO_CLOUD=1` set — CLAUDE.md's no-cloud-inference constraint requires
it disabled, and the script is the one place that's enforced. `llm/client.py`
adds a second, client-side check on top of this (rejects non-loopback hosts
and any `-cloud`-suffixed model tag), and
`tests/test_network_offline_guard.py` is an automated regression test for
the whole chain, run against the real server if one is up.

## Run the app

```bash
streamlit run app/main.py
```

**Run this from the project root** (i.e. `cd` here first — the command
above assumes it). Confirmed live: Streamlit resolves `.streamlit/config.toml`
(the file that disables its default telemetry and interactive email prompt)
relative to the process's working directory, not the script's location —
launching from anywhere else silently drops that config and Streamlit falls
back to its default phone-home behavior, which violates CLAUDE.md's
no-telemetry constraint. `app/main.py`'s own sys.path fix (needed so
`from app.auth import ...` resolves at all) doesn't have this problem — it's
anchored to the script's file location, not the working directory — but the
telemetry config does.

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and set
a real `chief_passphrase` first — see `app/auth.py`.
