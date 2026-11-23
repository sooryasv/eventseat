"""The 20-way live concurrency proof (SPEC §7.7).

Twenty independent sessions, one per seeded race account, call the real hold path at once.
The winning hold is always released in a `finally`, so the seat and the accounts are reusable.
"""

import asyncio
import time
import uuid
from collections import Counter
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import AppError, DemoBusy, EventNotFound, NotFound, SeatNotFound
from app.models import Booking, BookingSeat, Event, Seat, User
from app.schemas import HoldRequest, SimulationResult
from app.seed import RACE_USER_COUNT, race_email
from app.services.booking_hold_service import hold_seats
from app.services.booking_lifecycle_service import cancel_booking

log = structlog.get_logger()

# Per-process on purpose: documented as a single-instance limitation (ADR-002).
DEMO_LOCK = asyncio.Lock()

SessionFactory = async_sessionmaker[AsyncSession]


async def _race_user_ids(
    session_factory: SessionFactory, event_id: UUID, seat_id: UUID
) -> list[UUID]:
    async with session_factory() as session:
        if await session.scalar(select(Event.id).where(Event.id == event_id)) is None:
            raise EventNotFound("Event not found")
        seat_stmt = select(Seat.id).where(Seat.id == seat_id, Seat.event_id == event_id)
        if await session.scalar(seat_stmt) is None:
            raise SeatNotFound("Seat does not belong to this event")
        emails = [race_email(n) for n in range(1, RACE_USER_COUNT + 1)]
        ids = list((await session.scalars(select(User.id).where(User.email.in_(emails)))).all())
    if len(ids) != RACE_USER_COUNT:
        raise NotFound("Race demo accounts are not seeded")
    return ids


async def _one_request(
    session_factory: SessionFactory, user_id: UUID, payload: HoldRequest, ttl_seconds: int
) -> int:
    async with session_factory() as session:
        try:
            result = await hold_seats(
                session,
                user_id=user_id,
                payload=payload,
                idempotency_key=f"race-{uuid.uuid4()}",
                request_id="req_simulator",
                ttl_seconds=ttl_seconds,
            )
        except AppError as exc:
            return exc.status_code
        return result.status_code


async def _count_active_rows(session_factory: SessionFactory, seat_id: UUID) -> int:
    stmt = (
        select(func.count())
        .select_from(BookingSeat)
        .where(BookingSeat.seat_id == seat_id, BookingSeat.is_active.is_(True))
    )
    async with session_factory() as session:
        return (await session.execute(stmt)).scalar_one()


async def _release_race_holds(
    session_factory: SessionFactory, event_id: UUID, user_ids: list[UUID]
) -> int:
    stmt = select(Booking.id, Booking.user_id).where(
        Booking.event_id == event_id, Booking.user_id.in_(user_ids), Booking.status == "HELD"
    )
    async with session_factory() as session:
        holds = (await session.execute(stmt)).all()
    released = 0
    for booking_id, user_id in holds:
        async with session_factory() as session:
            try:
                await cancel_booking(
                    session, actor_id=user_id, is_admin=False, booking_id=booking_id
                )
            except AppError:
                log.warning("simulator_release_failed", booking_id=str(booking_id))
                continue
        released += 1
    return released


async def run_simulation(
    session_factory: SessionFactory, *, event_id: UUID, seat_id: UUID, ttl_seconds: int
) -> SimulationResult:
    if DEMO_LOCK.locked():
        raise DemoBusy("A simulation is already running")
    async with DEMO_LOCK:
        user_ids = await _race_user_ids(session_factory, event_id, seat_id)
        payload = HoldRequest(event_id=event_id, seat_ids=[seat_id])
        started = time.perf_counter()
        try:
            statuses = await asyncio.gather(
                *(_one_request(session_factory, uid, payload, ttl_seconds) for uid in user_ids)
            )
            duration_ms = round((time.perf_counter() - started) * 1000)
            active_rows = await _count_active_rows(session_factory, seat_id)
        finally:
            released = await _release_race_holds(session_factory, event_id, user_ids)
    return SimulationResult(
        status_counts=dict(Counter(str(status) for status in statuses)),
        double_bookings=max(0, active_rows - 1),
        active_seat_rows=active_rows,
        duration_ms=duration_ms,
        released_holds=released,
    )
