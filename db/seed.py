"""Toy seed data for local development and tests.

All names, contacts, and dates below are fictional. This module must never
read from or be pointed at Resident_Schedules/ or any other real roster
data — see CLAUDE.md's PII boundary section.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from db.models import (
    Assignment,
    AuditLog,
    Block,
    CallHistory,
    Resident,
    Rotation,
    Rule,
    TimeOff,
    get_engine,
    get_session,
    init_db,
)

RESIDENTS = [
    # name, pgy, start_date, end_date, contact, board_eligibility
    ("Alice Chen", 1, dt.date(2026, 7, 1), None, "achen@example.edu", False),
    ("Brian Osei", 1, dt.date(2026, 7, 1), None, "bosei@example.edu", False),
    ("Carla Nguyen", 1, dt.date(2026, 7, 1), None, "cnguyen@example.edu", False),
    ("David Kim", 1, dt.date(2026, 7, 1), None, "dkim@example.edu", False),
    ("Elena Petrov", 2, dt.date(2025, 7, 1), None, "epetrov@example.edu", False),
    ("Farid Haddad", 2, dt.date(2025, 7, 1), None, "fhaddad@example.edu", False),
    ("Grace Okafor", 2, dt.date(2025, 7, 1), None, "gokafor@example.edu", False),
    ("Henry Wallace", 3, dt.date(2024, 7, 1), None, "hwallace@example.edu", True),
    ("Isabel Marin", 3, dt.date(2024, 7, 1), None, "imarin@example.edu", True),
    ("Jamal Foster", 3, dt.date(2024, 7, 1), None, "jfoster@example.edu", True),
]

ROTATIONS = [
    # name, location, intern_capacity, senior_capacity, requires_pgy
    ("Wards", "Duke North", 4, 2, None),
    ("ICU", "Duke North", 2, 2, None),
    ("Clinic", "Duke Outpatient Clinic", 3, 3, None),
    ("Elective", "Various", 2, 3, 2),
]

# 6 four-week blocks, AY2026-27, block 1 starting 2026-07-01.
BLOCK_LENGTH_DAYS = 28


def _build_blocks(year: int, count: int, first_start: dt.date) -> list[tuple[int, int, dt.date, dt.date]]:
    blocks = []
    start = first_start
    for block_number in range(1, count + 1):
        end = start + dt.timedelta(days=BLOCK_LENGTH_DAYS - 1)
        blocks.append((year, block_number, start, end))
        start = end + dt.timedelta(days=1)
    return blocks


BLOCKS = _build_blocks(2026, 6, dt.date(2026, 7, 1))

# (resident_name, rotation_name, role) — block 1 only, capacities respected exactly.
BLOCK_1_ASSIGNMENTS = [
    ("Alice Chen", "Wards", "intern"),
    ("Brian Osei", "Wards", "intern"),
    ("Carla Nguyen", "Wards", "intern"),
    ("David Kim", "Wards", "intern"),  # Wards intern capacity: 4/4 (at cap)
    ("Elena Petrov", "Wards", "senior"),
    ("Farid Haddad", "Wards", "senior"),  # Wards senior capacity: 2/2 (at cap)
    ("Grace Okafor", "ICU", "senior"),
    ("Henry Wallace", "ICU", "senior"),  # ICU senior capacity: 2/2 (at cap)
    ("Isabel Marin", "Clinic", "senior"),  # Clinic senior capacity: 1/3
    ("Jamal Foster", "Elective", "senior"),  # Elective senior capacity: 1/3
]

TIME_OFF = [
    # resident_name, start_date, end_date, type, approved
    ("Carla Nguyen", dt.date(2026, 7, 29), dt.date(2026, 8, 2), "vacation", True),
    ("Isabel Marin", dt.date(2026, 7, 10), dt.date(2026, 7, 12), "sick", False),
]

CALL_HISTORY = [
    # resident_name, date, shift_type, hours
    ("Elena Petrov", dt.date(2026, 7, 8), "night_call", 14.0),
    ("Henry Wallace", dt.date(2026, 7, 12), "weekend_call", 24.0),
]

RULES = [
    {
        "name": "duty_hours",
        "version": 1,
        "definition": '{"max_hours_per_shift": 24, "max_hours_per_week": 80, "min_hours_off_between_shifts": 8}',
        "active": True,
    },
]


def seed(session: Session) -> None:
    residents_by_name: dict[str, Resident] = {}
    for name, pgy, start_date, end_date, contact, board_eligibility in RESIDENTS:
        resident = Resident(
            name=name,
            pgy=pgy,
            start_date=start_date,
            end_date=end_date,
            contact=contact,
            board_eligibility=board_eligibility,
        )
        session.add(resident)
        residents_by_name[name] = resident

    rotations_by_name: dict[str, Rotation] = {}
    for name, location, intern_capacity, senior_capacity, requires_pgy in ROTATIONS:
        rotation = Rotation(
            name=name,
            location=location,
            intern_capacity=intern_capacity,
            senior_capacity=senior_capacity,
            requires_pgy=requires_pgy,
        )
        session.add(rotation)
        rotations_by_name[name] = rotation

    blocks_by_number: dict[int, Block] = {}
    for year, block_number, start_date, end_date in BLOCKS:
        block = Block(
            year=year,
            block_number=block_number,
            start_date=start_date,
            end_date=end_date,
        )
        session.add(block)
        blocks_by_number[block_number] = block

    session.flush()

    block_1 = blocks_by_number[1]
    for resident_name, rotation_name, role in BLOCK_1_ASSIGNMENTS:
        session.add(
            Assignment(
                resident=residents_by_name[resident_name],
                block=block_1,
                rotation=rotations_by_name[rotation_name],
                role=role,
            )
        )

    for resident_name, start_date, end_date, type_, approved in TIME_OFF:
        session.add(
            TimeOff(
                resident=residents_by_name[resident_name],
                start_date=start_date,
                end_date=end_date,
                type=type_,
                approved=approved,
            )
        )

    for resident_name, date, shift_type, hours in CALL_HISTORY:
        session.add(
            CallHistory(
                resident=residents_by_name[resident_name],
                date=date,
                shift_type=shift_type,
                hours=hours,
            )
        )

    for rule in RULES:
        session.add(Rule(**rule))

    session.add(
        AuditLog(
            actor="system",
            action="seed_database",
            reason="initial toy dataset for prototype development",
            details=f"seeded {len(RESIDENTS)} residents, {len(ROTATIONS)} rotations, "
            f"{len(BLOCKS)} blocks, {len(BLOCK_1_ASSIGNMENTS)} assignments",
        )
    )

    session.commit()


def main() -> None:
    engine = get_engine()
    init_db(engine)
    with get_session(engine) as session:
        seed(session)
        print(f"residents: {session.query(Resident).count()}")
        print(f"rotations: {session.query(Rotation).count()}")
        print(f"blocks: {session.query(Block).count()}")
        print(f"assignments: {session.query(Assignment).count()}")
        print(f"time_off: {session.query(TimeOff).count()}")
        print(f"call_history: {session.query(CallHistory).count()}")
        print(f"rules: {session.query(Rule).count()}")
        print(f"audit_log: {session.query(AuditLog).count()}")


if __name__ == "__main__":
    main()
