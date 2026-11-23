"""Booking routes. Idempotent outcomes (incl. persisted 402/409/410) are returned as stored."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import AppSettings, CurrentUser, DbSession, IdempotencyKey
from app.schemas import (
    BookingCancelOut,
    ConfirmRequest,
    ConfirmResponse,
    HoldRequest,
    HoldResponse,
    MyBookingsOut,
)
from app.services import queries
from app.services.booking_hold_service import hold_seats
from app.services.booking_lifecycle_service import cancel_booking, confirm_booking
from app.services.idempotency import IdempotentResult

router = APIRouter(prefix="/bookings", tags=["bookings"])


def _respond(result: IdempotentResult) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(result.body, status_code=result.status_code, headers=headers)


@router.post("/hold", status_code=201, response_model=HoldResponse)
async def hold(
    payload: HoldRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    result = await hold_seats(
        db,
        user_id=user.id,
        payload=payload,
        idempotency_key=idempotency_key,
        request_id=request.state.request_id,
        ttl_seconds=settings.hold_ttl_seconds,
    )
    return _respond(result)


@router.post("/{booking_id}/confirm", response_model=ConfirmResponse)
async def confirm(
    booking_id: UUID,
    payload: ConfirmRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    result = await confirm_booking(
        db,
        user_id=user.id,
        booking_id=booking_id,
        payload=payload,
        idempotency_key=idempotency_key,
        request_id=request.state.request_id,
    )
    return _respond(result)


@router.post("/{booking_id}/cancel", response_model=BookingCancelOut)
async def cancel(booking_id: UUID, user: CurrentUser, db: DbSession) -> BookingCancelOut:
    return await cancel_booking(
        db, actor_id=user.id, is_admin=user.role == "SUPER_ADMIN", booking_id=booking_id
    )


@router.get("/me", response_model=MyBookingsOut)
async def my_bookings(
    user: CurrentUser, db: DbSession, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> MyBookingsOut:
    return await queries.my_bookings(db, user.id, limit)
