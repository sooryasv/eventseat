"""Event creation (atomic with its seat grid), management authorisation, and cancellation."""

from uuid import UUID

from sqlalchemy import DateTime, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import sql_now
from app.errors import EventNotFound, Forbidden, OrganizerNotVerified, ValidationFailed
from app.models import Booking, BookingSeat, Event, Seat
from app.schemas import EventCancelOut, EventCreate, EventOut, UserOut
from app.services.booking_hold_service import ACTIVE_STATUSES, lock_event, lock_seats

ROW_LABELS = "ABCDEFGHIJ"


def seat_grid(rows: int, cols: int) -> list[tuple[str, str, int]]:
    """(seat_label, row_label, seat_number) for a rows x cols grid, e.g. ('A1', 'A', 1)."""
    return [
        (f"{ROW_LABELS[row]}{number}", ROW_LABELS[row], number)
        for row in range(rows)
        for number in range(1, cols + 1)
    ]


async def create_event(session: AsyncSession, user: UserOut, data: EventCreate) -> EventOut:
    if user.role == "ATTENDEE":
        raise Forbidden("Only organizers can create events")
    if user.role == "ORGANIZER" and not user.is_verified_organizer:
        raise OrganizerNotVerified("Your organizer account has not been verified yet")

    # Both comparisons run on the SQL clock; Python time is never consulted.
    tz = DateTime(timezone=True)
    starts_ok, closes_ok = (
        await session.execute(
            select(
                literal(data.starts_at, tz) > sql_now(),
                literal(data.booking_closes_at, tz) > sql_now(),
            )
        )
    ).one()
    if not starts_ok:
        raise ValidationFailed("starts_at must be in the future")
    if not closes_ok:
        raise ValidationFailed("booking_closes_at must be in the future")

    event = Event(
        organizer_id=user.id,
        title=data.title,
        category=data.category,
        venue=data.venue,
        starts_at=data.starts_at,
        booking_closes_at=data.booking_closes_at,
    )
    session.add(event)
    await session.flush()
    await session.execute(
        insert(Seat),
        [
            {"event_id": event.id, "seat_label": label, "row_label": row, "seat_number": number}
            for label, row, number in seat_grid(data.rows, data.cols)
        ],
    )
    await session.commit()
    return EventOut.model_validate(event)


async def load_managed_event(session: AsyncSession, event_id: UUID, user: UserOut) -> Event:
    """404 if missing, then 403 unless the user organizes it (or is SUPER_ADMIN). No locks."""
    event = (await session.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
    if event is None:
        raise EventNotFound("Event not found")
    if user.role != "SUPER_ADMIN" and event.organizer_id != user.id:
        raise Forbidden("You do not manage this event")
    return event


async def cancel_event(session: AsyncSession, event_id: UUID, user: UserOut) -> EventCancelOut:
    async with session.begin():
        await load_managed_event(session, event_id, user)  # 404/403 before any lock

    async with session.begin():
        event = await lock_event(session, event_id, exclusive=True)  # L3
        if event is None:
            raise EventNotFound("Event not found")
        if event.status == "CANCELLED":
            return EventCancelOut(event_id=event_id, status="CANCELLED")
        await lock_seats(session, event_id, None)  # L4
        active = (
            select(Booking.id)
            .where(Booking.event_id == event_id, Booking.status.in_(ACTIVE_STATUSES))
            .order_by(Booking.id)
            .with_for_update()  # L5: bookings
        )
        booking_ids = list((await session.scalars(active)).all())
        if booking_ids:
            await session.execute(
                update(BookingSeat)
                .where(BookingSeat.booking_id.in_(booking_ids))
                .values(is_active=False)
            )
            await session.execute(
                update(Booking)
                .where(Booking.id.in_(booking_ids))
                .values(status="CANCELLED", cancelled_at=sql_now())
            )
        await session.execute(update(Event).where(Event.id == event_id).values(status="CANCELLED"))
        return EventCancelOut(event_id=event_id, status="CANCELLED")
