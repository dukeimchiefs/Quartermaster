"""SQLAlchemy 2.0 models mirroring db/schema.sql.

Schema changes must be made in both places — schema.sql is the DDL of
record (portable to Postgres), models.py is what application code imports.
tests/test_db_schema.py checks both stay in sync.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Engine,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Resident(Base):
    __tablename__ = "residents"
    __table_args__ = (CheckConstraint("pgy IN (1, 2, 3, 4)", name="ck_residents_pgy"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    pgy: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[dt.date] = mapped_column(nullable=False)
    end_date: Mapped[dt.date | None] = mapped_column(default=None)
    contact: Mapped[str | None] = mapped_column(String, default=None)
    board_eligibility: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    assignments: Mapped[list["Assignment"]] = relationship(back_populates="resident")
    time_off: Mapped[list["TimeOff"]] = relationship(back_populates="resident")
    call_history: Mapped[list["CallHistory"]] = relationship(back_populates="resident")


class Rotation(Base):
    __tablename__ = "rotations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    location: Mapped[str | None] = mapped_column(String, default=None)
    intern_capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    senior_capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requires_pgy: Mapped[int | None] = mapped_column(Integer, default=None)

    assignments: Mapped[list["Assignment"]] = relationship(back_populates="rotation")


class Block(Base):
    __tablename__ = "blocks"
    __table_args__ = (UniqueConstraint("year", "block_number", name="uq_blocks_year_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    block_number: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[dt.date] = mapped_column(nullable=False)
    end_date: Mapped[dt.date] = mapped_column(nullable=False)

    assignments: Mapped[list["Assignment"]] = relationship(back_populates="block")


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("resident_id", "block_id", name="uq_assignments_resident_block"),
        CheckConstraint("role IN ('intern', 'senior')", name="ck_assignments_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    resident_id: Mapped[int] = mapped_column(ForeignKey("residents.id"), nullable=False)
    block_id: Mapped[int] = mapped_column(ForeignKey("blocks.id"), nullable=False)
    rotation_id: Mapped[int] = mapped_column(ForeignKey("rotations.id"), nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)

    resident: Mapped["Resident"] = relationship(back_populates="assignments")
    block: Mapped["Block"] = relationship(back_populates="assignments")
    rotation: Mapped["Rotation"] = relationship(back_populates="assignments")


class TimeOff(Base):
    __tablename__ = "time_off"

    id: Mapped[int] = mapped_column(primary_key=True)
    resident_id: Mapped[int] = mapped_column(ForeignKey("residents.id"), nullable=False)
    start_date: Mapped[dt.date] = mapped_column(nullable=False)
    end_date: Mapped[dt.date] = mapped_column(nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    resident: Mapped["Resident"] = relationship(back_populates="time_off")


class CallHistory(Base):
    __tablename__ = "call_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    resident_id: Mapped[int] = mapped_column(ForeignKey("residents.id"), nullable=False)
    date: Mapped[dt.date] = mapped_column(nullable=False)
    shift_type: Mapped[str] = mapped_column(String, nullable=False)
    hours: Mapped[float] = mapped_column(nullable=False)

    resident: Mapped["Resident"] = relationship(back_populates="call_history")


class Swap(Base):
    __tablename__ = "swaps"

    id: Mapped[int] = mapped_column(primary_key=True)
    original_assignment_id: Mapped[int] = mapped_column(
        ForeignKey("assignments.id"), nullable=False
    )
    new_assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assignments.id"), default=None
    )
    reason: Mapped[str | None] = mapped_column(String, default=None)
    approved_by: Mapped[str | None] = mapped_column(String, default=None)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    original_assignment: Mapped["Assignment"] = relationship(foreign_keys=[original_assignment_id])
    new_assignment: Mapped["Assignment | None"] = relationship(foreign_keys=[new_assignment_id])


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    definition: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String, default=None)
    details: Mapped[str | None] = mapped_column(String, default=None)


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_engine(url: str = "sqlite:///resident_scheduler.db"):
    return create_engine(url)


def init_db(engine) -> None:
    Base.metadata.create_all(engine)


def get_session(engine) -> Session:
    return Session(engine)
