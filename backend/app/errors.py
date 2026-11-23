"""Typed domain errors, the SPEC §8 error envelope, and exception handlers."""

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger()


class AppError(Exception):
    """Base for every error that is rendered as the stable envelope."""

    status_code = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.code.replace("_", " ").capitalize()
        super().__init__(self.message)


class Unauthenticated(AppError):
    status_code = 401
    code = "UNAUTHENTICATED"


class PaymentSimulationFailed(AppError):
    status_code = 402
    code = "PAYMENT_SIMULATION_FAILED"


class Forbidden(AppError):
    status_code = 403
    code = "FORBIDDEN"


class OrganizerNotVerified(AppError):
    status_code = 403
    code = "ORGANIZER_NOT_VERIFIED"


class CsrfOriginRejected(AppError):
    status_code = 403
    code = "CSRF_ORIGIN_REJECTED"


class NotFound(AppError):
    status_code = 404
    code = "NOT_FOUND"


class EventNotFound(AppError):
    status_code = 404
    code = "EVENT_NOT_FOUND"


class SeatNotFound(AppError):
    status_code = 404
    code = "SEAT_NOT_FOUND"


class BookingNotFound(AppError):
    status_code = 404
    code = "BOOKING_NOT_FOUND"


class SeatUnavailable(AppError):
    status_code = 409
    code = "SEAT_UNAVAILABLE"


class UserLimitExceeded(AppError):
    status_code = 409
    code = "USER_LIMIT_EXCEEDED"


class IdempotencyMismatch(AppError):
    status_code = 409
    code = "IDEMPOTENCY_MISMATCH"


class BookingClosed(AppError):
    status_code = 409
    code = "BOOKING_CLOSED"


class EventNotPublished(AppError):
    status_code = 409
    code = "EVENT_NOT_PUBLISHED"


class BookingAlreadyConfirmed(AppError):
    status_code = 409
    code = "BOOKING_ALREADY_CONFIRMED"


class BookingCancelled(AppError):
    status_code = 409
    code = "BOOKING_CANCELLED"


class BookingAlreadyExpired(AppError):
    status_code = 409
    code = "BOOKING_ALREADY_EXPIRED"


class EventAlreadyStarted(AppError):
    status_code = 409
    code = "EVENT_ALREADY_STARTED"


class EmailAlreadyRegistered(AppError):
    status_code = 409
    code = "EMAIL_ALREADY_REGISTERED"


class BookingHoldExpired(AppError):
    status_code = 410
    code = "BOOKING_HOLD_EXPIRED"


class ValidationFailed(AppError):
    status_code = 422
    code = "VALIDATION_ERROR"


class DemoBusy(AppError):
    status_code = 429
    code = "DEMO_BUSY"


class InternalError(AppError):
    """Rendered for any unexpected failure; the message never leaks internals."""

    status_code = 500
    code = "INTERNAL_ERROR"


# Last line of defence: constraints sit behind the locks, never in front of them.
CONSTRAINT_ERRORS: dict[str, type[AppError]] = {
    "uq_booking_seats_active_seat": SeatUnavailable,
    "uq_bookings_one_active_hold_per_user_event": UserLimitExceeded,
    "users_email_key": EmailAlreadyRegistered,
}


def map_integrity_error(exc: IntegrityError) -> AppError | None:
    detail = str(exc.orig)
    for constraint_name, error_cls in CONSTRAINT_ERRORS.items():
        if constraint_name in detail:
            return error_cls()
    return None


def error_body(exc: AppError, request_id: str) -> dict[str, object]:
    return {"error": {"code": exc.code, "message": exc.message, "request_id": request_id}}


def _envelope(status_code: int, code: str, message: str, request_id: str) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "request_id": request_id}}
    return JSONResponse(body, status_code=status_code, headers={"X-Request-ID": request_id})


def error_response(exc: AppError, request_id: str) -> JSONResponse:
    return _envelope(exc.status_code, exc.code, exc.message, request_id)


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "req_unknown"))


async def _handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    error = exc if isinstance(exc, AppError) else InternalError("Internal server error")
    return error_response(error, _request_id(request))


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    message = "Invalid request"
    if isinstance(exc, RequestValidationError) and exc.errors():
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"] if part != "body")
        message = f"{field}: {first['msg']}" if field else str(first["msg"])
    return error_response(ValidationFailed(message), _request_id(request))


async def _handle_http_error(request: Request, exc: Exception) -> JSONResponse:
    status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
    codes = {401: "UNAUTHENTICATED", 403: "FORBIDDEN"}
    return _envelope(
        status, codes.get(status, "NOT_FOUND"), "Resource not found", _request_id(request)
    )


async def _handle_integrity_error(request: Request, exc: Exception) -> JSONResponse:
    mapped = map_integrity_error(exc) if isinstance(exc, IntegrityError) else None
    if mapped is None:
        return await _handle_unexpected(request, exc)
    return error_response(mapped, _request_id(request))


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled_exception", path=request.url.path, exc_info=exc)
    return error_response(InternalError("Internal server error"), _request_id(request))


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_error)
    app.add_exception_handler(IntegrityError, _handle_integrity_error)
    app.add_exception_handler(Exception, _handle_unexpected)
