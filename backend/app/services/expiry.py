"""Hold-expiry primitives and the background sweeper.

Correctness never depends on the sweeper: reads ignore lapsed holds and booking transactions
expire them lazily. The sweeper only keeps the tables tidy.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import sql_now
from app.models import Booking, BookingSeat, IdempotencyRecord

log = structlog.get_logger()

SWEEP_BOOKING_LIMIT = 100
SWEEP_IDEMPOTENCY_LIMIT = 500


@dataclass(frozen=True)
class SweepResult:
    expired_holds: int
    pruned_idempotency_records: int


async def mark_expired(session: AsyncSession, booking_ids: Sequence[UUID]) -> None:
    """Flip bookings to EXPIRED and free their seats. Caller must already hold L5 on them."""
    if not booking_ids:
        return
    await session.execute(
        update(BookingSeat).where(BookingSeat.booking_id.in_(booking_ids)).values(is_active=False)
    )
    await session.execute(
        update(Booking).where(Booking.id.in_(booking_ids)).values(status="EXPIRED")
    )


async def expire_stale_holds(session: AsyncSession, booking_ids: Sequence[UUID]) -> list[UUID]:
    """Lazily expire the lapsed HELD bookings among booking_ids; returns the ones expired.

    Caller must already hold L5 on booking_ids. The lapse test runs on the SQL clock.
    """
    if not booking_ids:
        return []
    stale = select(Booking.id).where(
        Booking.id.in_(booking_ids), Booking.status == "HELD", Booking.held_until <= sql_now()
    )
    expired = list((await session.scalars(stale)).all())
    await mark_expired(session, expired)
    return expired


async def run_sweeper_once(session_factory: async_sessionmaker[AsyncSession]) -> SweepResult:
    async with session_factory() as session, session.begin():
        # L5 only: SKIP LOCKED means the sweeper never waits on, or deadlocks with, a live request.
        stale = (
            select(Booking.id)
            .where(Booking.status == "HELD", Booking.held_until <= sql_now())
            .order_by(Booking.id)
            .limit(SWEEP_BOOKING_LIMIT)
            .with_for_update(skip_locked=True)
        )
        expired_ids = list((await session.scalars(stale)).all())
        await mark_expired(session, expired_ids)

        # SKIP LOCKED here too: a hold request may be pruning the same record under its L1 lock,
        # and waiting on it while holding booking locks could close a lock cycle.
        old_records = (
            select(IdempotencyRecord.id)
            .where(IdempotencyRecord.expires_at <= sql_now())
            .limit(SWEEP_IDEMPOTENCY_LIMIT)
            .with_for_update(skip_locked=True)
        )
        pruned = await session.scalars(
            delete(IdempotencyRecord)
            .where(IdempotencyRecord.id.in_(old_records))
            .returning(IdempotencyRecord.id)
        )
        return SweepResult(len(expired_ids), len(pruned.all()))


async def sweeper_loop(
    session_factory: async_sessionmaker[AsyncSession], interval_seconds: int
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            result = await run_sweeper_once(session_factory)
        except (SQLAlchemyError, OSError):
            log.exception("sweeper_failed")
            continue
        log.info(
            "sweeper_pass",
            expired_holds=result.expired_holds,
            pruned_idempotency_records=result.pruned_idempotency_records,
        )
