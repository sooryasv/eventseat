"""Confirm and cancel a booking (SPEC §7.4).

Order is always: unlocked read -> 404 -> authorisation 403 -> take locks -> re-validate under lock.
Authorisation failures therefore never wait on, or interfere with, anyone else's locks.
"""

import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import sql_now
from app.errors import (
    AppError,
    BookingAlreadyConfirmed,
    BookingAlreadyExpired,
    BookingCancelled,
    BookingHoldExpired,
    BookingNotFound,
    EventAlreadyStarted,
    EventNotFound,
    EventNotPublished,
    Forbidden,
    PaymentSimulationFailed,
)
from app.models import Booking, BookingSeat, Event
from app.schemas import BookingCancelOut, ConfirmRequest, ConfirmResponse
from app.services.booking_hold_service import acquire_hoard_lock, lock_event, lock_seats
from app.services.expiry import expire_stale_holds
from app.services.idempotency import (
    IdempotencyScope,
    IdempotentResult,
    begin_idempotent_request,
    confirm_request_hash,
    is_persistable,
    store_error,
    store_result,
)


@dataclass(frozen=True)
class BookingRef:
    owner_id: UUID
    event_id: UUID
    seat_ids: list[UUID]


async def read_booking_ref(session: AsyncSession, booking_id: UUID) -> BookingRef:
    """Plain unlocked read; raises 404 when the booking does not exist."""
    async with session.begin():
        head = (
            await session.execute(
                select(Booking.user_id, Booking.event_id).where(Booking.id == booking_id)
            )
        ).one_or_none()
        if head is None:
            raise BookingNotFound("Booking not found")
        seat_stmt = select(BookingSeat.seat_id).where(BookingSeat.booking_id == booking_id)
        seat_ids = list((await session.scalars(seat_stmt)).all())
    return BookingRef(head.user_id, head.event_id, seat_ids)


async def _lock_booking(session: AsyncSession, booking_id: UUID) -> Booking:
    stmt = select(Booking).where(Booking.id == booking_id).with_for_update()  # L5: booking
    result = await session.execute(stmt.execution_options(populate_existing=True))
    return result.scalar_one()


async def _decide_confirm(
    session: AsyncSession, event: Event, booking: Booking, outcome: str
) -> ConfirmResponse:
    """Runs inside a savepoint; every refusal is a typed error that rolls it back."""
    if event.status != "PUBLISHED":
        raise EventNotPublished("Event is no longer open")
    if booking.status == "CONFIRMED":
        raise BookingAlreadyConfirmed("Booking is already confirmed")
    if booking.status == "CANCELLED":
        raise BookingCancelled("Booking was cancelled")
    if booking.status == "EXPIRED":
        raise BookingHoldExpired("The hold expired and the seats were released")
    if outcome == "FAILURE":
        raise PaymentSimulationFailed("Simulated payment failed; your hold is still active")

    # held_until is re-checked inside the UPDATE itself so a hold can never be confirmed late.
    stmt = (
        update(Booking)
        .where(Booking.id == booking.id, Booking.status == "HELD", Booking.held_until > sql_now())
        .values(
            status="CONFIRMED",
            ticket_code=f"EVT-{secrets.token_hex(8).upper()}",
            confirmed_at=sql_now(),
        )
        .returning(Booking.ticket_code, Booking.confirmed_at)
    )
    confirmed = (await session.execute(stmt)).one_or_none()
    if confirmed is None:
        raise BookingHoldExpired("The hold expired and the seats were released")
    return ConfirmResponse(
        booking_id=booking.id,
        event_id=booking.event_id,
        status="CONFIRMED",
        ticket_code=confirmed.ticket_code,
        confirmed_at=confirmed.confirmed_at,
    )


async def confirm_booking(
    session: AsyncSession,
    *,
    user_id: UUID,
    booking_id: UUID,
    payload: ConfirmRequest,
    idempotency_key: str,
    request_id: str,
) -> IdempotentResult:
    ref = await read_booking_ref(session, booking_id)
    if ref.owner_id != user_id:
        raise Forbidden("This booking belongs to someone else")
    scope = IdempotencyScope(
        user_id=user_id,
        endpoint=f"confirm:{booking_id}",
        key=idempotency_key,
        request_hash=confirm_request_hash(booking_id, payload.simulate_outcome),
    )
    async with session.begin():
        replay = await begin_idempotent_request(session, scope)  # L1
        if replay is not None:
            return replay
        await acquire_hoard_lock(session, ref.owner_id, ref.event_id)  # L2
        event = await lock_event(session, ref.event_id, exclusive=False)  # L3
        if event is None:
            raise EventNotFound("Event not found")
        await lock_seats(session, ref.event_id, ref.seat_ids)  # L4
        booking = await _lock_booking(session, booking_id)  # L5
        if booking.user_id != user_id:
            raise Forbidden("This booking belongs to someone else")

        # A lapsed hold is expired for real and the 410 is committed together with it (no rollback).
        is_open_hold = event.status == "PUBLISHED" and booking.status == "HELD"
        if is_open_hold and await expire_stale_holds(session, [booking.id]):
            expired = BookingHoldExpired("The hold expired and the seats were released")
            return await store_error(session, scope, expired, request_id)
        try:
            async with session.begin_nested():
                confirmed = await _decide_confirm(session, event, booking, payload.simulate_outcome)
        except AppError as exc:
            if not is_persistable(exc.status_code):
                raise
            return await store_error(session, scope, exc, request_id)
        return await store_result(session, scope, 200, confirmed.model_dump(mode="json"))


async def _started(session: AsyncSession, event_id: UUID) -> bool:
    stmt = select(sql_now() >= Event.starts_at).where(Event.id == event_id)
    return bool((await session.execute(stmt)).scalar_one())


async def _apply_cancel(session: AsyncSession, booking: Booking) -> AppError | None:
    """Returns the refusal to raise AFTER commit, so lazy expiry is persisted before the 409."""
    if booking.status == "CANCELLED":
        return None
    if booking.status == "EXPIRED":
        return BookingAlreadyExpired("The hold already expired")
    if booking.status == "HELD" and await expire_stale_holds(session, [booking.id]):
        return BookingAlreadyExpired("The hold already expired")
    if await _started(session, booking.event_id):
        return EventAlreadyStarted("The event has already started")
    await session.execute(
        update(BookingSeat).where(BookingSeat.booking_id == booking.id).values(is_active=False)
    )
    await session.execute(
        update(Booking)
        .where(Booking.id == booking.id)
        .values(status="CANCELLED", cancelled_at=sql_now())
    )
    return None


async def cancel_booking(
    session: AsyncSession, *, actor_id: UUID, is_admin: bool, booking_id: UUID
) -> BookingCancelOut:
    ref = await read_booking_ref(session, booking_id)
    if ref.owner_id != actor_id and not is_admin:
        raise Forbidden("This booking belongs to someone else")
    async with session.begin():
        await acquire_hoard_lock(session, ref.owner_id, ref.event_id)  # L2
        if await lock_event(session, ref.event_id, exclusive=False) is None:  # L3
            raise EventNotFound("Event not found")
        await lock_seats(session, ref.event_id, ref.seat_ids)  # L4
        booking = await _lock_booking(session, booking_id)  # L5
        if booking.user_id != actor_id and not is_admin:
            raise Forbidden("This booking belongs to someone else")
        refusal = await _apply_cancel(session, booking)
    if refusal is not None:
        raise refusal
    return BookingCancelOut(booking_id=booking_id, status="CANCELLED")
