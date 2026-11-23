"""The six tables. Constraint names mirror migration 0001 exactly (SPEC §6)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TZ = DateTime(timezone=True)
NOW = text("NOW()")


class Base(DeclarativeBase):
    pass


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


def uuid_fk(target: str) -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, ondelete="RESTRICT"))


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('ATTENDEE','ORGANIZER','SUPER_ADMIN')", name="users_role_check"),
        CheckConstraint("email = LOWER(email)", name="ck_users_email_lowercase"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(255), unique=True)
    full_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    is_verified_organizer: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("FALSE")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "category IN ('CONFERENCE','WORKSHOP','MEETUP','SEMINAR')", name="events_category_check"
        ),
        CheckConstraint("status IN ('PUBLISHED','CANCELLED')", name="events_status_check"),
        CheckConstraint("booking_closes_at <= starts_at", name="ck_events_cutoff_before_start"),
        Index("ix_events_status_starts", "status", "starts_at"),
        Index("ix_events_organizer", "organizer_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organizer_id: Mapped[uuid.UUID] = uuid_fk("users.id")
    title: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(40))
    venue: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(TZ)
    booking_closes_at: Mapped[datetime] = mapped_column(TZ)
    status: Mapped[str] = mapped_column(
        String(20), default="PUBLISHED", server_default=text("'PUBLISHED'")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class Seat(Base):
    __tablename__ = "seats"
    __table_args__ = (
        CheckConstraint("seat_number > 0", name="seats_seat_number_check"),
        UniqueConstraint("event_id", "seat_label", name="uq_seats_event_label"),
        UniqueConstraint("id", "event_id", name="uq_seats_id_event"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    event_id: Mapped[uuid.UUID] = uuid_fk("events.id")
    seat_label: Mapped[str] = mapped_column(String(16))
    row_label: Mapped[str] = mapped_column(String(8))
    seat_number: Mapped[int] = mapped_column(Integer)


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint(
            "status IN ('HELD','CONFIRMED','CANCELLED','EXPIRED')", name="bookings_status_check"
        ),
        UniqueConstraint("id", "event_id", name="uq_bookings_id_event"),
        CheckConstraint(
            "status <> 'HELD' OR held_until IS NOT NULL", name="ck_bookings_held_has_expiry"
        ),
        CheckConstraint(
            "status <> 'CONFIRMED' OR (ticket_code IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="ck_bookings_confirmed_fields",
        ),
        CheckConstraint(
            "status <> 'CANCELLED' OR cancelled_at IS NOT NULL", name="ck_bookings_cancelled_fields"
        ),
        Index("ix_bookings_user_created", "user_id", text("created_at DESC")),
        Index("ix_bookings_event_status", "event_id", "status"),
        Index("ix_bookings_held_expiry", "held_until", postgresql_where=text("status = 'HELD'")),
        Index(
            "uq_bookings_one_active_hold_per_user_event",
            "user_id",
            "event_id",
            unique=True,
            postgresql_where=text("status = 'HELD'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = uuid_fk("users.id")
    event_id: Mapped[uuid.UUID] = uuid_fk("events.id")
    status: Mapped[str] = mapped_column(String(20))
    held_until: Mapped[datetime | None] = mapped_column(TZ)
    ticket_code: Mapped[str | None] = mapped_column(String(24), unique=True)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    confirmed_at: Mapped[datetime | None] = mapped_column(TZ)
    cancelled_at: Mapped[datetime | None] = mapped_column(TZ)


class BookingSeat(Base):
    __tablename__ = "booking_seats"
    __table_args__ = (
        ForeignKeyConstraint(
            ["booking_id", "event_id"],
            ["bookings.id", "bookings.event_id"],
            name="fk_booking_seats_booking_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["seat_id", "event_id"],
            ["seats.id", "seats.event_id"],
            name="fk_booking_seats_seat_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("booking_id", "seat_id", name="uq_booking_seats_booking_seat"),
        Index("ix_booking_seats_booking", "booking_id"),
        Index(
            "uq_booking_seats_active_seat",
            "event_id",
            "seat_id",
            unique=True,
            postgresql_where=text("is_active = TRUE"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    seat_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("TRUE"))


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint(
            "status_code >= 200 AND status_code < 500", name="idempotency_records_status_code_check"
        ),
        UniqueConstraint(
            "user_id", "endpoint", "idempotency_key", name="uq_idempotency_user_endpoint_key"
        ),
        Index("ix_idempotency_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = uuid_fk("users.id")
    endpoint: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    expires_at: Mapped[datetime] = mapped_column(
        TZ, server_default=text("(NOW() + INTERVAL '24 hours')")
    )
