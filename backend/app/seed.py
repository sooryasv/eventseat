"""Idempotent demo data: three personas, 20 race accounts, three events.

Every insert is ON CONFLICT DO NOTHING, so running this on every startup is safe.
"""

import secrets
import uuid
from datetime import timedelta

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import sql_now
from app.models import Event, Seat, User
from app.security import hash_password
from app.services.auth_service import DEMO_PERSONA_EMAILS
from app.services.event_service import seat_grid

log = structlog.get_logger()

RACE_USER_COUNT = 20
SEED_NAMESPACE = uuid.UUID("5f0d6b0e-1c1e-4c53-9d36-0c7b1a7d6a11")

# (title, category, venue, rows, cols). Dates are far out so a long-lived demo never goes stale.
DEMO_EVENTS = (
    ("Backend Systems Summit", "CONFERENCE", "Chennai Trade Centre", 5, 8),
    ("Postgres Concurrency Workshop", "WORKSHOP", "Coimbatore Tech Park", 6, 10),
    ("Indie Builders Meetup", "MEETUP", "Bengaluru Community Hub", 4, 6),
)


def race_email(number: int) -> str:
    return f"race-demo-{number:02d}@eventseat.demo"


def _stable_id(name: str) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, name)


def _user_rows(password_hash: str) -> list[dict[str, object]]:
    people = [
        (DEMO_PERSONA_EMAILS["ATTENDEE"], "Demo Attendee", "ATTENDEE", False),
        (DEMO_PERSONA_EMAILS["ORGANIZER"], "Demo Organizer", "ORGANIZER", True),
        (DEMO_PERSONA_EMAILS["SUPER_ADMIN"], "Demo Admin", "SUPER_ADMIN", False),
    ]
    people += [
        (race_email(n), f"Race Demo {n:02d}", "ATTENDEE", False)
        for n in range(1, RACE_USER_COUNT + 1)
    ]
    return [
        {
            "id": _stable_id(email),
            "email": email,
            "full_name": name,
            "password_hash": password_hash,
            "role": role,
            "is_verified_organizer": verified,
        }
        for email, name, role, verified in people
    ]


def _event_and_seat_rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    organizer_id = _stable_id(DEMO_PERSONA_EMAILS["ORGANIZER"])
    events: list[dict[str, object]] = []
    seats: list[dict[str, object]] = []
    for offset, (title, category, venue, rows, cols) in enumerate(DEMO_EVENTS):
        event_id = _stable_id(f"event:{title}")
        events.append(
            {
                "id": event_id,
                "organizer_id": organizer_id,
                "title": title,
                "category": category,
                "venue": venue,
                "starts_at": sql_now() + timedelta(days=365 + offset),
                "booking_closes_at": sql_now() + timedelta(days=364 + offset),
            }
        )
        seats.extend(
            {
                "id": _stable_id(f"seat:{event_id}:{label}"),
                "event_id": event_id,
                "seat_label": label,
                "row_label": row,
                "seat_number": number,
            }
            for label, row, number in seat_grid(rows, cols)
        )
    return events, seats


async def run_seed(session_factory: async_sessionmaker[AsyncSession]) -> None:
    # The password is random and thrown away: demo accounts can only be entered through the
    # demo-login route, which is disabled when ENABLE_DEMO_TOOLS=false.
    password_hash = await hash_password(secrets.token_urlsafe(32))
    users = _user_rows(password_hash)
    events, seats = _event_and_seat_rows()
    async with session_factory() as session, session.begin():
        await session.execute(pg_insert(User).values(users).on_conflict_do_nothing())
        await session.execute(pg_insert(Event).values(events).on_conflict_do_nothing())
        await session.execute(pg_insert(Seat).values(seats).on_conflict_do_nothing())
    log.info("seed_complete", users=len(users), events=len(events))
