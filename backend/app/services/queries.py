"""Read models. The live-occupancy predicate is defined once, here, and reused everywhere."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Integer, and_, case, cast, extract, func, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db import sql_now
from app.errors import EventNotFound
from app.models import Booking, BookingSeat, Event, Seat, User
from app.schemas import (
    AnalyticsOut,
    BookingOut,
    EventListOut,
    EventOut,
    EventSummary,
    MyBookingsOut,
    RosterEntry,
    SeatMapOut,
    SeatOut,
    SeatStatus,
)


def seat_occupied_predicate() -> ColumnElement[bool]:
    """A seat is occupied only by a confirmed booking or a hold that has not lapsed.

    Evaluated against the SQL clock, so an expired-but-unswept hold never shows as taken.
    """
    live_hold = and_(Booking.status == "HELD", Booking.held_until > sql_now())
    return and_(BookingSeat.is_active.is_(True), or_(Booking.status == "CONFIRMED", live_hold))


def remaining_seconds_expr() -> ColumnElement[int]:
    seconds_left = extract("epoch", Booking.held_until - sql_now())
    return cast(func.greatest(0, func.ceil(seconds_left)), Integer)


def _ordered_seat_labels() -> ColumnElement[Sequence[str]]:
    return func.array_agg(aggregate_order_by(Seat.seat_label, Seat.row_label, Seat.seat_number))


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def catalog(
    session: AsyncSession, *, q: str | None, category: str | None, page: int, page_size: int
) -> EventListOut:
    total_seats = (
        select(func.count())
        .select_from(Seat)
        .where(Seat.event_id == Event.id)
        .correlate(Event)
        .scalar_subquery()
    )
    occupied = (
        select(func.count())
        .select_from(BookingSeat)
        .join(Booking, Booking.id == BookingSeat.booking_id)
        .where(BookingSeat.event_id == Event.id, seat_occupied_predicate())
        .correlate(Event)
        .scalar_subquery()
    )
    stmt = select(
        Event,
        total_seats.label("total_seats"),
        occupied.label("occupied"),
        func.count().over().label("total_rows"),
    ).where(Event.starts_at > sql_now())
    if q:
        pattern = _like_pattern(q)
        stmt = stmt.where(or_(Event.title.ilike(pattern, "\\"), Event.venue.ilike(pattern, "\\")))
    if category:
        stmt = stmt.where(Event.category == category)
    stmt = stmt.order_by(Event.starts_at, Event.id).limit(page_size).offset((page - 1) * page_size)

    rows = (await session.execute(stmt)).all()
    items = [
        EventSummary(
            **EventOut.model_validate(row.Event).model_dump(),
            total_seats=row.total_seats,
            available_seats=0
            if row.Event.status == "CANCELLED"
            else row.total_seats - row.occupied,
        )
        for row in rows
    ]
    total = rows[0].total_rows if rows else 0
    return EventListOut(items=items, page=page, page_size=page_size, total=total)


async def seat_map(session: AsyncSession, event_id: UUID, viewer_id: UUID | None) -> SeatMapOut:
    event = (await session.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
    if event is None:
        raise EventNotFound("Event not found")

    occupant = (
        select(
            BookingSeat.seat_id.label("seat_id"),
            Booking.user_id.label("user_id"),
            Booking.status.label("status"),
        )
        .join(Booking, Booking.id == BookingSeat.booking_id)
        .where(BookingSeat.event_id == event_id, seat_occupied_predicate())
        .subquery()
    )
    stmt = (
        select(Seat, occupant.c.user_id, occupant.c.status)
        .outerjoin(occupant, occupant.c.seat_id == Seat.id)
        .where(Seat.event_id == event_id)
        .order_by(Seat.row_label, Seat.seat_number)
    )
    seats: list[SeatOut] = []
    for seat, occupant_id, occupant_status in (await session.execute(stmt)).all():
        status: SeatStatus = "AVAILABLE"
        if occupant_id is not None:
            if viewer_id is not None and occupant_id == viewer_id:
                status = "MINE"
            else:
                status = "BOOKED" if occupant_status == "CONFIRMED" else "HELD"
        seats.append(
            SeatOut(
                id=seat.id,
                seat_label=seat.seat_label,
                row_label=seat.row_label,
                seat_number=seat.seat_number,
                status=status,
            )
        )
    return SeatMapOut(event=EventOut.model_validate(event), seats=seats)


async def analytics(session: AsyncSession, event: Event) -> AnalyticsOut:
    total = (
        await session.execute(
            select(func.count()).select_from(Seat).where(Seat.event_id == event.id)
        )
    ).scalar_one()
    counts = (
        await session.execute(
            select(
                func.count().filter(Booking.status == "CONFIRMED"),
                func.count().filter(Booking.status == "HELD"),
            )
            .select_from(BookingSeat)
            .join(Booking, Booking.id == BookingSeat.booking_id)
            .where(BookingSeat.event_id == event.id, seat_occupied_predicate())
        )
    ).one()
    confirmed, held = counts[0], counts[1]
    roster_stmt = (
        select(Booking.id, User.full_name, User.email, Booking.confirmed_at, _ordered_seat_labels())
        .select_from(Booking)
        .join(User, User.id == Booking.user_id)
        .join(BookingSeat, BookingSeat.booking_id == Booking.id)
        .join(Seat, Seat.id == BookingSeat.seat_id)
        .where(
            Booking.event_id == event.id,
            Booking.status == "CONFIRMED",
            BookingSeat.is_active.is_(True),
        )
        .group_by(Booking.id, User.full_name, User.email, Booking.confirmed_at)
        .order_by(Booking.confirmed_at, Booking.id)
    )
    roster = [
        RosterEntry(
            booking_id=booking_id,
            full_name=full_name,
            email=email,
            confirmed_at=confirmed_at,
            seat_labels=labels,
        )
        for booking_id, full_name, email, confirmed_at, labels in (
            await session.execute(roster_stmt)
        ).all()
    ]
    cancelled = event.status == "CANCELLED"
    return AnalyticsOut(
        event_id=event.id,
        total_seats=total,
        available=0 if cancelled else total - held - confirmed,
        held=held,
        confirmed=confirmed,
        utilization_pct=round(confirmed * 100 / total, 1) if total else 0.0,
        roster=roster,
    )


async def my_bookings(session: AsyncSession, user_id: UUID, limit: int) -> MyBookingsOut:
    effective_status = case(
        (and_(Booking.status == "HELD", Booking.held_until <= sql_now()), "EXPIRED"),
        else_=Booking.status,
    )
    stmt = (
        select(
            Booking,
            Event.title,
            Event.starts_at,
            effective_status.label("effective_status"),
            remaining_seconds_expr().label("remaining"),
            _ordered_seat_labels().label("labels"),
        )
        .join(Event, Event.id == Booking.event_id)
        .join(BookingSeat, BookingSeat.booking_id == Booking.id)
        .join(Seat, Seat.id == BookingSeat.seat_id)
        .where(Booking.user_id == user_id)
        .group_by(Booking.id, Event.title, Event.starts_at)
        .order_by(Booking.created_at.desc(), Booking.id)
        .limit(limit)
    )
    result = MyBookingsOut(tickets=[], holds=[], history=[])
    for row in (await session.execute(stmt)).all():
        booking: Booking = row.Booking
        is_live_hold = row.effective_status == "HELD"
        out = BookingOut(
            id=booking.id,
            event_id=booking.event_id,
            event_title=row.title,
            event_starts_at=row.starts_at,
            status=row.effective_status,
            held_until=booking.held_until if is_live_hold else None,
            remaining_seconds=row.remaining if is_live_hold else None,
            ticket_code=booking.ticket_code,
            seat_labels=row.labels,
            created_at=booking.created_at,
            confirmed_at=booking.confirmed_at,
            cancelled_at=booking.cancelled_at,
        )
        if row.effective_status == "CONFIRMED":
            result.tickets.append(out)
        elif is_live_hold:
            result.holds.append(out)
        else:
            result.history.append(out)
    return result
