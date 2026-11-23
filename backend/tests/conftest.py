"""Shared fixtures. Every test runs against a real, Alembic-migrated PostgreSQL (never SQLite)."""

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import CursorResult, Row, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.main import create_app
from app.security import COOKIE_NAME, create_access_token
from app.services.event_service import seat_grid

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres@localhost:5433/eventseat_test"
)
ORIGIN = "http://localhost:5173"
API = "/api/v1"
TABLES = "users, events, seats, bookings, booking_seats, idempotency_records"

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class Actor:
    id: uuid.UUID
    headers: dict[str, str]


@dataclass(frozen=True)
class EventFx:
    id: uuid.UUID
    seats: dict[str, uuid.UUID]


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest_asyncio.fixture
async def stack() -> AsyncIterator[AsyncExitStack]:
    async with AsyncExitStack() as exit_stack:
        yield exit_stack


@pytest.fixture
def make_app(stack: AsyncExitStack) -> Callable[..., Awaitable[FastAPI]]:
    async def factory(**overrides: object) -> FastAPI:
        values: dict[str, object] = {
            "database_url": TEST_DATABASE_URL,
            "run_seed_on_startup": False,
            "sweeper_interval_seconds": 3600,
            "jwt_secret": "test-secret-test-secret-test-secret",
        }
        app = create_app(Settings(_env_file=None, **{**values, **overrides}))  # type: ignore[arg-type]
        await stack.enter_async_context(app.router.lifespan_context(app))
        async with app.state.engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        return app

    return factory


@pytest.fixture
def make_client(stack: AsyncExitStack) -> Callable[..., Awaitable[httpx.AsyncClient]]:
    async def factory(app: FastAPI, origin: str | None = ORIGIN) -> httpx.AsyncClient:
        headers = {"Origin": origin} if origin else {}
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers=headers,
        )
        return await stack.enter_async_context(client)

    return factory


@pytest_asyncio.fixture
async def app(make_app: Callable[..., Awaitable[FastAPI]]) -> FastAPI:
    return await make_app()


@pytest_asyncio.fixture
async def client(
    app: FastAPI, make_client: Callable[..., Awaitable[httpx.AsyncClient]]
) -> httpx.AsyncClient:
    return await make_client(app)


@pytest.fixture
def session_factory(app: FastAPI) -> SessionFactory:
    factory: SessionFactory = app.state.session_factory
    return factory


@pytest.fixture
def sql(session_factory: SessionFactory) -> Callable[..., Awaitable[list[Row[tuple[object, ...]]]]]:
    async def run(statement: str, **params: object) -> list[Row[tuple[object, ...]]]:
        async with session_factory() as session, session.begin():
            result = await session.execute(text(statement), params)
            if isinstance(result, CursorResult) and result.returns_rows:
                return list(result.all())
            return []

    return run


@pytest.fixture
def create_user(
    app: FastAPI, sql: Callable[..., Awaitable[list[Row[tuple[object, ...]]]]]
) -> Callable[..., Awaitable[Actor]]:
    async def make(role: str = "ATTENDEE", *, verified: bool = False) -> Actor:
        user_id = uuid.uuid4()
        await sql(
            "INSERT INTO users (id, email, full_name, password_hash, role, is_verified_organizer)"
            " VALUES (:id, :email, 'Test User', 'not-a-real-hash', :role, :verified)",
            id=user_id,
            email=f"{role.lower()}-{user_id.hex[:10]}@test.dev",
            role=role,
            verified=verified,
        )
        token = create_access_token(user_id, app.state.settings)
        return Actor(user_id, {"Cookie": f"{COOKIE_NAME}={token}"})

    return make


@pytest.fixture
def create_event(
    sql: Callable[..., Awaitable[list[Row[tuple[object, ...]]]]],
) -> Callable[..., Awaitable[EventFx]]:
    async def make(
        organizer_id: uuid.UUID,
        *,
        rows: int = 2,
        cols: int = 4,
        title: str = "Test Event",
        category: str = "MEETUP",
        starts_in: timedelta = timedelta(days=30),
        closes_in: timedelta = timedelta(days=29),
    ) -> EventFx:
        event_id = uuid.uuid4()
        await sql(
            "INSERT INTO events (id, organizer_id, title, category, venue, starts_at,"
            " booking_closes_at) VALUES (:id, :org, :title, :category, 'Test Hall',"
            " statement_timestamp() + CAST(:starts AS interval),"
            " statement_timestamp() + CAST(:closes AS interval))",
            id=event_id,
            org=organizer_id,
            title=title,
            category=category,
            starts=starts_in,
            closes=closes_in,
        )
        seats = {label: uuid.uuid4() for label, _, _ in seat_grid(rows, cols)}
        for label, row_label, number in seat_grid(rows, cols):
            await sql(
                "INSERT INTO seats (id, event_id, seat_label, row_label, seat_number)"
                " VALUES (:id, :event, :label, :row, :number)",
                id=seats[label],
                event=event_id,
                label=label,
                row=row_label,
                number=number,
            )
        return EventFx(event_id, seats)

    return make


@pytest.fixture
def hold(client: httpx.AsyncClient) -> Callable[..., Awaitable[httpx.Response]]:
    async def do(
        actor: Actor, event: EventFx, labels: list[str], key: str | None = None
    ) -> httpx.Response:
        return await client.post(
            f"{API}/bookings/hold",
            json={"event_id": str(event.id), "seat_ids": [str(event.seats[x]) for x in labels]},
            headers={**actor.headers, "Idempotency-Key": key or str(uuid.uuid4())},
        )

    return do


@pytest.fixture
def confirm(client: httpx.AsyncClient) -> Callable[..., Awaitable[httpx.Response]]:
    async def do(
        actor: Actor, booking_id: str, outcome: str = "SUCCESS", key: str | None = None
    ) -> httpx.Response:
        return await client.post(
            f"{API}/bookings/{booking_id}/confirm",
            json={"simulate_outcome": outcome},
            headers={**actor.headers, "Idempotency-Key": key or str(uuid.uuid4())},
        )

    return do
