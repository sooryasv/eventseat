"""FastAPI dependencies: sessions, settings, and cookie-based authentication."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import Forbidden, NotFound, Unauthenticated
from app.schemas import UserOut
from app.security import COOKIE_NAME, decode_access_token
from app.services.auth_service import get_user


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_optional_user(request: Request) -> UserOut | None:
    """Looks the user up in its own short-lived session, so no connection is held by the route.

    The simulator relies on this: it fans out 20 sessions and must not pin one for itself.
    """
    token = request.cookies.get(COOKIE_NAME)
    user_id = decode_access_token(token, get_app_settings(request)) if token else None
    if user_id is None:
        return None
    async with request.app.state.session_factory() as session:
        user: UserOut | None = await get_user(session, user_id)
        return user


async def get_current_user(request: Request) -> UserOut:
    user = await get_optional_user(request)
    if user is None:
        raise Unauthenticated("Authentication required")
    return user


async def require_super_admin(user: Annotated[UserOut, Depends(get_current_user)]) -> UserOut:
    if user.role != "SUPER_ADMIN":
        raise Forbidden("Super admin only")
    return user


def require_demo_tools(settings: Annotated[Settings, Depends(get_app_settings)]) -> None:
    if not settings.enable_demo_tools:
        raise NotFound("Not found")


DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]
CurrentUser = Annotated[UserOut, Depends(get_current_user)]
OptionalUser = Annotated[UserOut | None, Depends(get_optional_user)]
SuperAdmin = Annotated[UserOut, Depends(require_super_admin)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]
