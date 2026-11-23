"""App factory: middleware, routers, lifespan (engine, seed, sweeper), SPA mount, /healthz."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api import admin, auth, bookings, events
from app.api.deps import DbSession
from app.config import Settings, get_settings
from app.db import build_engine, make_session_factory
from app.errors import NotFound, register_error_handlers
from app.observability import RequestIdMiddleware, configure_logging
from app.security import OriginGuardMiddleware
from app.seed import run_seed
from app.services.expiry import sweeper_loop

# backend/app/main.py -> repo root. The Docker image mirrors this layout.
STATIC_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    engine = build_engine(settings.database_url, use_null_pool=settings.use_null_pool)
    session_factory = make_session_factory(engine)
    app.state.engine = engine
    app.state.session_factory = session_factory
    # Migrations are a release step (alembic upgrade head), never part of startup.
    if settings.run_seed_on_startup:
        await run_seed(session_factory)
    sweeper = asyncio.create_task(sweeper_loop(session_factory, settings.sweeper_interval_seconds))
    try:
        yield
    finally:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
        await engine.dispose()


def _mount_frontend(app: FastAPI) -> None:
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        return
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    async def spa(path: str) -> FileResponse:
        if path.startswith(("api/", "healthz")):
            raise NotFound("Resource not found")
        candidate = (STATIC_DIR / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(STATIC_DIR.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    app = FastAPI(title="EventSeat", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings or get_settings()

    # Added first = inner. RequestId is added last so it wraps everything, including the CSRF guard.
    app.add_middleware(OriginGuardMiddleware, allowed_origins=app.state.settings.origins)
    app.add_middleware(RequestIdMiddleware, demo_tools_enabled=app.state.settings.enable_demo_tools)
    register_error_handlers(app)

    api_v1 = APIRouter(prefix="/api/v1")
    for module in (auth, events, bookings, admin):
        api_v1.include_router(module.router)
    app.include_router(api_v1)

    @app.get("/healthz", response_model=dict[str, str])
    async def healthz(db: DbSession) -> dict[str, str]:
        await db.execute(text("SELECT 1"))
        return {"status": "ok"}

    _mount_frontend(app)
    return app
