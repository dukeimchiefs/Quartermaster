# Resident Scheduling Assistant

A fully local (offline / on-prem) tool to help a chief resident build the IM
residency block schedule, handle call-outs and swaps, and revise the schedule
mid-cycle. See `CLAUDE.md` for full architecture and design decisions.

## Status

**Prototype.** Governance sign-off from Duke AI/data governance and the GME
office is pending — this must not influence official schedules until that
review is complete.

Currently implemented: DB schema, SQLAlchemy models, toy seed data, and
directory scaffolding. Solver, LLM, UI, and audit modules are stubbed with
`NotImplementedError` placeholders pointing at their Development Priority
number in `CLAUDE.md`.

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
