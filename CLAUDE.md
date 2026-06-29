# Resident Scheduling Assistant — Project Context

## Overview

A fully local (offline / on-prem) tool to help a chief resident:

1. **Build** the IM residency block schedule from scratch (1–2× per year).
2. **Handle call-outs and swaps** quickly when residents are unavailable (daily–weekly, sometimes urgent).
3. **Revise mid-cycle** when residents join/leave or rotations change.

The system handles identifiable workforce data (resident names, rotations, locations, contact info). It must never make outbound network calls in production. All compute, storage, and inference runs on a single on-prem workstation.

---

## Non-Negotiable Privacy & Governance Constraints

- **No cloud inference, no external APIs, no telemetry.** Set `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `WANDB_MODE=disabled`. Audit every dependency for phone-home behavior.
- **Full-disk encryption** (LUKS / FileVault) on any machine that touches the database or model weights.
- **OS-level auth + auto-lock.** No shared logins.
- **Audit log.** Every schedule change the system proposes or commits must be written to an append-only log with timestamp, actor, and reason.
- **Backups encrypted at rest** with the same posture as the live DB.
- **Governance sign-off pending:** Duke AI/data governance office and the GME office should review before this influences official schedules. Treat current build as prototype until that's done.
- **PII boundary:** model fine-tuning datasets contain real names and rotations — same handling rules as the production DB.

---

## Architecture

```
┌─────────────────────────────┐
│   Local Postgres / SQLite   │  ← residents, rotations, rules, history, audit log
└──────────────┬──────────────┘
               │
      ┌────────┴────────┐
      │                 │
┌─────▼─────────┐  ┌────▼──────────┐
│  Solvers      │  │  Local LLM    │
│  (OR-Tools)   │  │  (Ollama)     │
│  - full       │  │  Single model │
│  - repair     │  │  Two prompts  │
│  - warm-start │  │               │
└─────┬─────────┘  └────┬──────────┘
      │                 │
      └────────┬────────┘
               │
        ┌──────▼──────┐
        │  Streamlit  │  ← three pages, all local
        └─────────────┘
```

**Key principle:** the solver owns correctness; the LLM owns the interface. Never let the LLM generate schedule assignments directly — it pattern-matches and silently violates hard constraints.

---

## Tech Stack

- Python 3.11+
- **Solver:** Google OR-Tools (`ortools.sat.python.cp_model`, CP-SAT)
- **Storage:** Postgres (preferred) or SQLite for prototype; SQLAlchemy
- **LLM runtime:** Ollama serving an instruct model (Llama 3.1 8B or Qwen 2.5 14B to start)
- **Fine-tuning (later, if needed):** Unsloth + QLoRA, see prior notes
- **UI:** Streamlit (local-only, no external CDN)
- **Logging:** stdlib `logging` + append-only audit log to disk; no cloud loggers

---

## Solver Design — Two Distinct Models, Shared Rules

The two scheduling problems are structurally different. Keep them separate.

### `solver/full_schedule.py` — Ground-up

- Variables: `assignment[resident][block][rotation] ∈ {0,1}`
- Hard constraints: required rotations, rotation capacities, ACGME duty hours, approved vacation, board eligibility, intern/senior coverage ratios
- Objective: balance call burden, satisfy preferences, distribute hardship fairly
- Solve budget: minutes–hours
- Frequency: 1–2× per year

### `solver/repair.py` — Call-out / swap

- Variables: only the **changes** to an existing valid schedule
- Hard constraints: cover the open shift, don't blow duty hours of replacement, no double coverage, no cascading constraint violations
- Objective: **minimize disruption** — fewest people moved, fewest extra hours, fairness in absorbing the gap
- Solve budget: seconds (it's 6am, someone's sick)
- Frequency: daily–weekly

### `solver/warm_start.py` — Mid-cycle revisions

- Reuses `full_schedule.py` but with a penalty term on deviations from the current schedule
- Use case: resident takes leave, new intern joins, rotation requirement changes

### `solver/rules.py` — Shared

All three solvers import constraint definitions from here. Single source of truth for ACGME rules and program-specific policies. Changes here propagate everywhere.

**Public API:**

```python
build_full_schedule(roster, year, preferences) -> Schedule
repair_schedule(current_schedule, open_shift, sick_resident) -> list[SwapProposal]
revise_schedule(current_schedule, perturbations) -> Schedule
```

---

## LLM Layer — One Model, Two Prompts

A single local instruct model handles all natural-language work. Do **not** maintain separate fine-tuned models for schedule building vs. call-outs.

### What the LLM does

- Parses free-text call-outs ("Sarah is out tomorrow, possibly Thursday — flu")
- Translates between user intent and solver inputs / outputs
- Explains why a swap is infeasible (translates solver's infeasibility output into prose)
- Drafts communications (pages, Slack messages, emails to PD)
- Summarizes coverage and changes

### What the LLM must not do

- Generate schedule assignments directly
- Override solver output
- Be the final word on whether a swap is legal — always cross-check against `rules.py`

### Prompts

- `llm/prompts/schedule_builder.md` — system prompt for full-schedule UI
- `llm/prompts/callout_handler.md` — system prompt for call-out UI
- `llm/tools.py` — function-calling interface to solvers, DB queries, rule explainer

### Fine-tuning posture

Fine-tuning is **deferred** until prompting + retrieval over the rules doc proves insufficient. When/if it happens:

- Use Unsloth + QLoRA
- Mixed dataset covering both task types (don't split)
- ~200–500 curated examples to start
- All PII-handling rules apply to the training data

---

## Repository Structure

```
resident-scheduler/
  solver/
    __init__.py
    rules.py              # shared constraint definitions
    full_schedule.py      # ground-up CP-SAT model
    repair.py             # call-out CP-SAT model
    warm_start.py         # mid-cycle revisions
  llm/
    prompts/
      schedule_builder.md
      callout_handler.md
    tools.py              # function-calling glue
    client.py             # Ollama wrapper
  db/
    schema.sql
    models.py             # SQLAlchemy
    migrations/
  app/
    main.py
    pages/
      1_Build_Schedule.py
      2_Call_Out.py
      3_Review_Changes.py
  audit/
    log.py                # append-only audit logger
  tests/
    test_rules.py
    test_repair.py
    test_full_schedule.py
  CLAUDE.md
  README.md
  requirements.txt
```

---

## Database Schema (sketch)

- `residents` — id, name, pgy, start_date, end_date, contact, board_eligibility
- `rotations` — id, name, location, intern_capacity, senior_capacity, requires_pgy
- `blocks` — id, year, block_number, start_date, end_date
- `assignments` — resident_id, block_id, rotation_id, role
- `time_off` — resident_id, start_date, end_date, type, approved
- `call_history` — resident_id, date, shift_type, hours
- `swaps` — id, original_assignment, new_assignment, reason, approved_by, timestamp
- `rules` — versioned rule definitions
- `audit_log` — append-only

---

## Development Priorities

Build in this order. The call-out solver has daily ROI and smaller scope — prove the architecture there before tackling the annual schedule.

1. **DB schema + seed data** — toy roster of 10 residents, 4 rotations, 6 blocks
2. **`rules.py`** — start with 3–4 hard rules (duty hours, no double-coverage, required rotation, vacation respect)
3. **`repair.py` (call-out solver)** — get one realistic scenario working end-to-end
4. **Streamlit page 2 (Call Out)** — structured form + result display
5. **Ollama integration** — single prompt, single tool (the repair solver)
6. **LLM-driven call-out parsing** — free-text → structured solver call
7. **Audit log wiring** — every proposed and committed change logged
8. **`full_schedule.py`** — ground-up scheduler
9. **Page 1 (Build Schedule)**
10. **Page 3 (Review Changes)** — diff viewer for proposed schedules
11. **`warm_start.py`** — mid-cycle revisions
12. **Hardening** — encryption check, offline check, governance review
13. **Fine-tuning** — only if prompting alone proves insufficient after real use

---

## Conventions & Guardrails for Code Changes

- All solver constraints route through `rules.py`. Don't inline a constraint in `repair.py` or `full_schedule.py`.
- LLM output that affects DB state must round-trip through the solver for validation. No direct DB writes from LLM tool calls.
- Every state change writes to `audit_log` before returning to the caller.
- No new dependencies without checking their network behavior (telemetry, auto-update, license-check phone-home).
- Tests for `rules.py` are the highest priority — a regression here is a real-world scheduling violation.
- Never log PII to stdout in production mode. Audit log is the only PII-permitted sink.

---

## Useful References

- [Google OR-Tools scheduling](https://developers.google.com/optimization/scheduling)
- [OR-Tools employee scheduling example](https://developers.google.com/optimization/scheduling/employee_scheduling)
- [Ollama](https://ollama.com)
- [Unsloth fine-tuning docs](https://unsloth.ai/docs/get-started/fine-tuning-llms-guide) (for the later fine-tuning phase)
- [ACGME Common Program Requirements](https://www.acgme.org/what-we-do/accreditation/common-program-requirements/)
- [Streamlit](https://streamlit.io)
