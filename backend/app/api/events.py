"""Event routes: catalog, creation, seat map, analytics, cancellation, simulator."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import AppSettings, CurrentUser, DbSession, OptionalUser, require_demo_tools
from app.schemas import (
    AnalyticsOut,
    Category,
    EventCancelOut,
    EventCreate,
    EventListOut,
    EventOut,
    SeatMapOut,
    SimulateRequest,
    SimulationResult,
)
from app.services import concurrency_demo, event_service, queries

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=EventListOut)
async def list_events(
    db: DbSession,
    q: Annotated[str | None, Query(max_length=100)] = None,
    category: Category | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 12,
) -> EventListOut:
    return await queries.catalog(db, q=q, category=category, page=page, page_size=page_size)


@router.post("", status_code=201, response_model=EventOut)
async def create_event(payload: EventCreate, user: CurrentUser, db: DbSession) -> EventOut:
    return await event_service.create_event(db, user, payload)


@router.get("/{event_id}/seats", response_model=SeatMapOut)
async def seat_map(event_id: UUID, viewer: OptionalUser, db: DbSession) -> SeatMapOut:
    return await queries.seat_map(db, event_id, viewer.id if viewer else None)


@router.get("/{event_id}/analytics", response_model=AnalyticsOut)
async def event_analytics(event_id: UUID, user: CurrentUser, db: DbSession) -> AnalyticsOut:
    event = await event_service.load_managed_event(db, event_id, user)
    return await queries.analytics(db, event)


@router.post("/{event_id}/cancel", response_model=EventCancelOut)
async def cancel_event(event_id: UUID, user: CurrentUser, db: DbSession) -> EventCancelOut:
    return await event_service.cancel_event(db, event_id, user)


@router.post(
    "/{event_id}/simulate-concurrency",
    response_model=SimulationResult,
    dependencies=[Depends(require_demo_tools)],
)
async def simulate_concurrency(
    event_id: UUID,
    payload: SimulateRequest,
    request: Request,
    _user: CurrentUser,
    settings: AppSettings,
) -> SimulationResult:
    # Deliberately no DbSession: the simulator opens its own 20 sessions and must not pin one.
    return await concurrency_demo.run_simulation(
        request.app.state.session_factory,
        event_id=event_id,
        seat_id=payload.seat_id,
        ttl_seconds=settings.hold_ttl_seconds,
    )
