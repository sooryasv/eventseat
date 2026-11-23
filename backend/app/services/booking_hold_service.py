"""Hold seats: the L1 -> L5 path (SPEC §7.4).

Also hosts the L2-L4 lock primitives that the lifecycle and event services share, so every
path acquires the same levels in the same ascending order.
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, func, insert, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import advisory_lock_key, advisory_xact_lock, sql_now
from app.errors import (
    AppError,
    BookingClosed,
    EventNotFound,
    EventNotPublished,
    SeatNotFound,
    SeatUnavailable,
    UserLimitExceeded,
    map_integrity_error,
)
from app.models import Booking, BookingSeat, Event, Seat
from app.schemas import HoldRequest, HoldResponse
from app.services.expiry import expire_stale_holds
from app.services.idempotency import (
    IdempotencyScope,
    IdempotentResult,
    begin_idempotent_request,
    hold_request_hash,
    is_persistable,
    store_error,
    store_result,
)
from app.services.queries import remaining_seconds_expr

HOLD_ENDPOINT = "hold"
ACTIVE_STATUSES = ("HELD", "CONFIRMED")
MAX_SEATS_PER_USER_EVENT = 4


async def acquire_hoard_lock(session: AsyncSession, owner_id: UUID, event_id: UUID) -> None:
    key = advisory_lock_key("hoard", str(owner_id), str(event_id))
    await advisory_xact_lock(session, key)  # L2: hoarding advisory


async def lock_event(session: AsyncSession, event_id: UUID, *, exclusive: bool) -> Event | None:
    # L3: events row, FOR SHARE for hold/confirm/cancel-booking, FOR UPDATE for cancel-event
    stmt = select(Event).where(Event.id == event_id).with_for_update(read=not exclusive)
    result = await session.execute(stmt.execution_options(populate_existing=True))
    return result.scalar_one_or_none()


async def lock_seats(
    session: AsyncSession, event_id: UUID, seat_ids: Sequence[UUID] | None
) -> list[tuple[UUID, str]]:
    """L4: seat rows, always ORDER BY id so two multi-seat holds can never deadlock."""
    stmt = select(Seat.id, Seat.seat_label).where(Seat.event_id == event_id)
    if seat_ids is not None:
        stmt = stmt.where(Seat.id.in_(seat_ids))
    rows = (await session.execute(stmt.order_by(Seat.id).with_for_update())).all()  # L4: seats
    return [(row.id, row.seat_label) for row in rows]


async def booking_is_open(session: AsyncSession, event_id: UUID) -> bool:
    stmt = select(sql_now() < Event.booking_closes_at).where(Event.id == event_id)
    return bool((await session.execute(stmt)).scalar_one())


async def _lock_conflicting_bookings(
    session: AsyncSession, user_id: UUID, event_id: UUID, seat_ids: Sequence[UUID]
) -> list[UUID]:
    holders_of_seats = select(BookingSeat.booking_id).where(
        BookingSeat.event_id == event_id,
        BookingSeat.seat_id.in_(seat_ids),
        BookingSeat.is_active.is_(True),
    )
    users_active = and_(
        Booking.user_id == user_id,
        Booking.event_id == event_id,
        Booking.status.in_(ACTIVE_STATUSES),
    )
    stmt = (
        select(Booking.id)
        .where(or_(Booking.id.in_(holders_of_seats), users_active))
        .order_by(Booking.id)
        .with_for_update()  # L5: bookings
    )
    return list((await session.scalars(stmt)).all())


async def _enforce_caps(
    session: AsyncSession, user_id: UUID, event_id: UUID, requested_count: int
) -> None:
    holds_stmt = (
        select(func.count())
        .select_from(Booking)
        .where(Booking.user_id == user_id, Booking.event_id == event_id, Booking.status == "HELD")
    )
    if (await session.execute(holds_stmt)).scalar_one() >= 1:
        raise UserLimitExceeded("You already have an active hold for this event")
    seats_stmt = (
        select(func.count())
        .select_from(BookingSeat)
        .join(Booking, Booking.id == BookingSeat.booking_id)
        .where(
            Booking.user_id == user_id,
            BookingSeat.event_id == event_id,
            BookingSeat.is_active.is_(True),
        )
    )
    active_seats = (await session.execute(seats_stmt)).scalar_one()
    if active_seats + requested_count > MAX_SEATS_PER_USER_EVENT:
        raise UserLimitExceeded(f"At most {MAX_SEATS_PER_USER_EVENT} seats per event")


async def _insert_hold(
    session: AsyncSession,
    *,
    event: Event,
    user_id: UUID,
    seats: list[tuple[UUID, str]],
    requested: list[UUID],
    ttl_seconds: int,
) -> HoldResponse:
    """Validate in SPEC order, then write. Runs inside a savepoint."""
    if event.status != "PUBLISHED":
        raise EventNotPublished("Event is not open for booking")
    if not await booking_is_open(session, event.id):
        raise BookingClosed("Booking has closed for this event")
    if len(seats) != len(requested):
        raise SeatNotFound("One or more seats do not belong to this event")
    taken_stmt = (
        select(func.count())
        .select_from(BookingSeat)
        .where(
            BookingSeat.event_id == event.id,
            BookingSeat.seat_id.in_(requested),
            BookingSeat.is_active.is_(True),
        )
    )
    if (await session.execute(taken_stmt)).scalar_one():
        raise SeatUnavailable("One or more seats are no longer available")
    await _enforce_caps(session, user_id, event.id, len(requested))

    booking_id = uuid.uuid4()
    created = await session.execute(
        insert(Booking)
        .values(
            id=booking_id,
            user_id=user_id,
            event_id=event.id,
            status="HELD",
            held_until=sql_now() + timedelta(seconds=ttl_seconds),
        )
        .returning(Booking.held_until, remaining_seconds_expr())
    )
    held_until, remaining_seconds = created.one()
    await session.execute(
        insert(BookingSeat),
        [{"booking_id": booking_id, "event_id": event.id, "seat_id": s} for s in requested],
    )
    labels = dict(seats)
    return HoldResponse(
        booking_id=booking_id,
        event_id=event.id,
        status="HELD",
        seat_ids=requested,
        seat_labels=[labels[s] for s in requested],
        held_until=held_until,
        remaining_seconds=remaining_seconds,
    )


async def hold_seats(
    session: AsyncSession,
    *,
    user_id: UUID,
    payload: HoldRequest,
    idempotency_key: str,
    request_id: str,
    ttl_seconds: int,
) -> IdempotentResult:
    scope = IdempotencyScope(
        user_id=user_id,
        endpoint=HOLD_ENDPOINT,
        key=idempotency_key,
        request_hash=hold_request_hash(payload.event_id, payload.seat_ids),
    )
    async with session.begin():
        replay = await begin_idempotent_request(session, scope)  # L1
        if replay is not None:
            return replay
        await acquire_hoard_lock(session, user_id, payload.event_id)  # L2
        event = await lock_event(session, payload.event_id, exclusive=False)  # L3
        if event is None:
            raise EventNotFound("Event not found")
        seats = await lock_seats(session, event.id, payload.seat_ids)  # L4
        locked = await _lock_conflicting_bookings(
            session, user_id, event.id, payload.seat_ids
        )  # L5
        # Outer transaction, before the savepoint: lapsed holds are freed even if this hold fails.
        await expire_stale_holds(session, locked)
        try:
            async with session.begin_nested():
                hold = await _insert_hold(
                    session,
                    event=event,
                    user_id=user_id,
                    seats=seats,
                    requested=payload.seat_ids,
                    ttl_seconds=ttl_seconds,
                )
        except IntegrityError as exc:
            mapped = map_integrity_error(exc)
            if mapped is None:
                raise
            error: AppError = mapped
        except AppError as exc:
            if not is_persistable(exc.status_code):
                raise
            error = exc
        else:
            return await store_result(session, scope, 201, hold.model_dump(mode="json"))
        # Deterministic 409: the savepoint is already rolled back; persist the outcome and commit.
        return await store_error(session, scope, error, request_id)
