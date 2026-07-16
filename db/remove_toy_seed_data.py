"""One-time cleanup: remove db/seed.py's fictional toy residents (and their
assignments/time-off/call-history) from a live DB that has since been
populated with real data via db.sync_real_schedule. Safe to re-run — a
no-op once the toy rows are already gone (e.g. if db/seed.py is
accidentally run again against a live, real-data DB, this cleans it back
up). Confirmed live (2026-07) against the actual resident_scheduler.db: no
committed Swap row references any toy resident's assignment, so this is a
plain delete, not a cascade needing special FK handling — SQLite's own
foreign-key enforcement (db/models.py's _enable_sqlite_foreign_keys) would
raise and roll back cleanly if that ever weren't true.

Run manually: `python -m db.remove_toy_seed_data`.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from db.models import Assignment, CallHistory, Resident, TimeOff, get_engine, get_session
from db.seed import RESIDENTS as _TOY_RESIDENT_FIXTURES

TOY_NAMES = [name for name, *_ in _TOY_RESIDENT_FIXTURES]


def remove_toy_seed_data(session: Session) -> dict[str, int]:
    toy_residents = session.query(Resident).filter(Resident.name.in_(TOY_NAMES)).all()
    resident_ids = [r.id for r in toy_residents]
    if not resident_ids:
        return {"residents": 0, "assignments": 0, "time_off": 0, "call_history": 0}

    assignments = session.query(Assignment).filter(Assignment.resident_id.in_(resident_ids)).all()
    time_off = session.query(TimeOff).filter(TimeOff.resident_id.in_(resident_ids)).all()
    call_history = session.query(CallHistory).filter(CallHistory.resident_id.in_(resident_ids)).all()

    summary = {
        "residents": len(toy_residents),
        "assignments": len(assignments),
        "time_off": len(time_off),
        "call_history": len(call_history),
    }

    # Delete dependents before the resident rows they reference.
    for row in [*assignments, *time_off, *call_history]:
        session.delete(row)
    for resident in toy_residents:
        session.delete(resident)
    session.commit()
    return summary


def main() -> None:
    engine = get_engine()
    with get_session(engine) as session:
        summary = remove_toy_seed_data(session)
        for key, value in summary.items():
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
