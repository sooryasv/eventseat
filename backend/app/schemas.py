"""Pydantic v2 request/response contracts (SPEC §8)."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Role = Literal["ATTENDEE", "ORGANIZER", "SUPER_ADMIN"]
Category = Literal["CONFERENCE", "WORKSHOP", "MEETUP", "SEMINAR"]
EventStatus = Literal["PUBLISHED", "CANCELLED"]
BookingStatus = Literal["HELD", "CONFIRMED", "CANCELLED", "EXPIRED"]
SeatStatus = Literal["AVAILABLE", "HELD", "BOOKED", "MINE"]


def _normalise_email(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


# No email-validator dependency (not on the approved list); a structural check is enough here.
Email = Annotated[
    str,
    BeforeValidator(_normalise_email),
    StringConstraints(max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
]
Password = Annotated[str, StringConstraints(min_length=8, max_length=128)]


class RequestModel(BaseModel):
    """Unknown fields are rejected, so a client can never smuggle in e.g. is_verified_organizer."""

    model_config = ConfigDict(extra="forbid")


# --- auth -----------------------------------------------------------------------------------


class RegisterRequest(RequestModel):
    email: Email
    password: Password
    full_name: str = Field(min_length=1, max_length=120)
    role: Literal["ATTENDEE", "ORGANIZER"] = "ATTENDEE"


class LoginRequest(RequestModel):
    email: Email
    password: str = Field(min_length=1, max_length=128)


class DemoLoginRequest(RequestModel):
    persona: Role


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    full_name: str
    role: Role
    is_verified_organizer: bool


class OrganizerVerifyRequest(RequestModel):
    is_verified: bool


class OkOut(BaseModel):
    ok: bool = True


# --- events ---------------------------------------------------------------------------------


class EventCreate(RequestModel):
    title: str = Field(min_length=1, max_length=160)
    category: Category
    venue: str = Field(min_length=1, max_length=200)
    starts_at: AwareDatetime
    booking_closes_at: AwareDatetime
    rows: int = Field(ge=1, le=10)
    cols: int = Field(ge=1, le=12)

    @model_validator(mode="after")
    def cutoff_not_after_start(self) -> "EventCreate":
        if self.booking_closes_at > self.starts_at:
            raise ValueError("booking_closes_at must not be after starts_at")
        return self


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organizer_id: UUID
    title: str
    category: Category
    venue: str
    starts_at: datetime
    booking_closes_at: datetime
    status: EventStatus


class EventSummary(EventOut):
    total_seats: int
    available_seats: int


class EventListOut(BaseModel):
    items: list[EventSummary]
    page: int
    page_size: int
    total: int


class SeatOut(BaseModel):
    id: UUID
    seat_label: str
    row_label: str
    seat_number: int
    status: SeatStatus


class SeatMapOut(BaseModel):
    event: EventOut
    seats: list[SeatOut]


class RosterEntry(BaseModel):
    booking_id: UUID
    full_name: str
    email: str
    seat_labels: list[str]
    confirmed_at: datetime


class AnalyticsOut(BaseModel):
    event_id: UUID
    total_seats: int
    available: int
    held: int
    confirmed: int
    utilization_pct: float
    roster: list[RosterEntry]


class EventCancelOut(BaseModel):
    event_id: UUID
    status: EventStatus


# --- bookings -------------------------------------------------------------------------------


class HoldRequest(RequestModel):
    event_id: UUID
    seat_ids: list[UUID] = Field(min_length=1, max_length=4)

    @field_validator("seat_ids")
    @classmethod
    def seat_ids_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("seat_ids must be unique")
        return value


class ConfirmRequest(RequestModel):
    simulate_outcome: Literal["SUCCESS", "FAILURE"]


class HoldResponse(BaseModel):
    booking_id: UUID
    event_id: UUID
    status: BookingStatus
    seat_ids: list[UUID]
    seat_labels: list[str]
    held_until: datetime
    remaining_seconds: int


class ConfirmResponse(BaseModel):
    booking_id: UUID
    event_id: UUID
    status: BookingStatus
    ticket_code: str
    confirmed_at: datetime


class BookingCancelOut(BaseModel):
    booking_id: UUID
    status: BookingStatus


class BookingOut(BaseModel):
    id: UUID
    event_id: UUID
    event_title: str
    event_starts_at: datetime
    status: BookingStatus
    held_until: datetime | None
    remaining_seconds: int | None
    ticket_code: str | None
    seat_labels: list[str]
    created_at: datetime
    confirmed_at: datetime | None
    cancelled_at: datetime | None


class MyBookingsOut(BaseModel):
    tickets: list[BookingOut]
    holds: list[BookingOut]
    history: list[BookingOut]


# --- demo -----------------------------------------------------------------------------------


class SimulateRequest(RequestModel):
    seat_id: UUID


class SimulationResult(BaseModel):
    status_counts: dict[str, int]
    double_bookings: int
    active_seat_rows: int
    duration_ms: int
    released_holds: int
