"""initial schema: six tables, partial unique indexes, composite foreign keys

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# One statement per execute(): asyncpg cannot prepare multiple commands at once.
UPGRADE_STATEMENTS = (
    """
    CREATE TABLE users (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        email VARCHAR(255) NOT NULL UNIQUE,
        full_name VARCHAR(120) NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        role VARCHAR(20) NOT NULL CHECK (role IN ('ATTENDEE','ORGANIZER','SUPER_ADMIN')),
        is_verified_organizer BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT ck_users_email_lowercase CHECK (email = LOWER(email))
    )
    """,
    """
    CREATE TABLE events (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        organizer_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        title VARCHAR(160) NOT NULL,
        category VARCHAR(40) NOT NULL CHECK (category IN ('CONFERENCE','WORKSHOP','MEETUP','SEMINAR')),
        venue VARCHAR(200) NOT NULL,
        starts_at TIMESTAMPTZ NOT NULL,
        booking_closes_at TIMESTAMPTZ NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'PUBLISHED' CHECK (status IN ('PUBLISHED','CANCELLED')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT ck_events_cutoff_before_start CHECK (booking_closes_at <= starts_at)
    )
    """,
    "CREATE INDEX ix_events_status_starts ON events (status, starts_at)",
    "CREATE INDEX ix_events_organizer ON events (organizer_id)",
    """
    CREATE TABLE seats (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        event_id UUID NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
        seat_label VARCHAR(16) NOT NULL,
        row_label VARCHAR(8) NOT NULL,
        seat_number INTEGER NOT NULL CHECK (seat_number > 0),
        CONSTRAINT uq_seats_event_label UNIQUE (event_id, seat_label),
        CONSTRAINT uq_seats_id_event UNIQUE (id, event_id)
    )
    """,
    """
    CREATE TABLE bookings (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        event_id UUID NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
        status VARCHAR(20) NOT NULL CHECK (status IN ('HELD','CONFIRMED','CANCELLED','EXPIRED')),
        held_until TIMESTAMPTZ,
        ticket_code VARCHAR(24) UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        confirmed_at TIMESTAMPTZ,
        cancelled_at TIMESTAMPTZ,
        CONSTRAINT uq_bookings_id_event UNIQUE (id, event_id),
        CONSTRAINT ck_bookings_held_has_expiry CHECK (status <> 'HELD' OR held_until IS NOT NULL),
        CONSTRAINT ck_bookings_confirmed_fields CHECK (status <> 'CONFIRMED' OR (ticket_code IS NOT NULL AND confirmed_at IS NOT NULL)),
        CONSTRAINT ck_bookings_cancelled_fields CHECK (status <> 'CANCELLED' OR cancelled_at IS NOT NULL)
    )
    """,
    "CREATE INDEX ix_bookings_user_created ON bookings (user_id, created_at DESC)",
    "CREATE INDEX ix_bookings_event_status ON bookings (event_id, status)",
    "CREATE INDEX ix_bookings_held_expiry ON bookings (held_until) WHERE status = 'HELD'",
    """
    CREATE UNIQUE INDEX uq_bookings_one_active_hold_per_user_event
        ON bookings (user_id, event_id) WHERE status = 'HELD'
    """,
    """
    CREATE TABLE booking_seats (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        booking_id UUID NOT NULL,
        event_id UUID NOT NULL,
        seat_id UUID NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        CONSTRAINT fk_booking_seats_booking_event
            FOREIGN KEY (booking_id, event_id) REFERENCES bookings(id, event_id) ON DELETE RESTRICT,
        CONSTRAINT fk_booking_seats_seat_event
            FOREIGN KEY (seat_id, event_id) REFERENCES seats(id, event_id) ON DELETE RESTRICT,
        CONSTRAINT uq_booking_seats_booking_seat UNIQUE (booking_id, seat_id)
    )
    """,
    "CREATE INDEX ix_booking_seats_booking ON booking_seats (booking_id)",
    """
    CREATE UNIQUE INDEX uq_booking_seats_active_seat
        ON booking_seats (event_id, seat_id) WHERE is_active = TRUE
    """,
    """
    CREATE TABLE idempotency_records (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        endpoint VARCHAR(64) NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL,
        request_hash CHAR(64) NOT NULL,
        status_code INTEGER NOT NULL CHECK (status_code >= 200 AND status_code < 500),
        response_body JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '24 hours'),
        CONSTRAINT uq_idempotency_user_endpoint_key UNIQUE (user_id, endpoint, idempotency_key)
    )
    """,
    "CREATE INDEX ix_idempotency_expires ON idempotency_records (expires_at)",
)

DOWNGRADE_TABLES = (
    "idempotency_records",
    "booking_seats",
    "bookings",
    "seats",
    "events",
    "users",
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for table in DOWNGRADE_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")  # noqa: S608 - fixed table names
