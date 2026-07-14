# Resident Scheduling Assistant

A fully local (offline / on-prem) tool to help a chief resident build the IM
residency block schedule, handle call-outs and swaps, and revise the schedule
mid-cycle. See `CLAUDE.md` for full architecture and design decisions.


## PII / data handling

`Resident_Schedules/` (if present locally) is real, live roster/schedule
data synced from Duke OneDrive and contains PII. It is git-ignored and
**must never be written to** — that remains absolute, unconditional, no
exceptions.

As of 2026-07, `real_schedule/` **is** authorized to *read* (never write)
these files at runtime, for the two schedule-verification tools (Check
Assist Swap, Check Clinic Coverage — see `app/pages/4_Check_Assist_Swap.py`
/ `5_Check_Clinic_Coverage.py`). This is a deliberate decision made
directly by the chief resident, reversing this project's earlier
blanket "never read" rule for that one package only. Every reader in
`real_schedule/` opens with `openpyxl.load_workbook(path, read_only=True,
data_only=True)` — `read_only=True` removes the write API from the
returned object entirely, which is the actual enforcement mechanism, not
just a comment (see `tests/test_real_schedule_never_writes.py`).

Everywhere else in this codebase, the original rule is unchanged and still
absolute: `db/seed.py` and the rest of the toy-DB-backed app must never
read from, write to, or be pointed at `Resident_Schedules/` as a data
source. All seed and test data in this repo (outside `real_schedule/`'s
own runtime reads of the live files) is fictional, and real data must never
enter git regardless of any of the above.

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
