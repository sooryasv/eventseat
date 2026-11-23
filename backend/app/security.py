"""Password hashing (off the event loop), JWT session cookie, and the Origin CSRF guard."""

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

import jwt
from fastapi import Response
from pwdlib import PasswordHash
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings
from app.errors import CsrfOriginRejected, error_response

COOKIE_NAME = "eventseat_session"
JWT_ALGORITHM = "HS256"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_hasher = PasswordHash.recommended()
# Verified against when the email is unknown, so response time does not reveal which emails exist.
_DUMMY_HASH = _hasher.hash("eventseat-dummy-password")


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password: str, password_hash: str | None) -> bool:
    if password_hash is None:
        await asyncio.to_thread(_hasher.verify, password, _DUMMY_HASH)
        return False
    return await asyncio.to_thread(_hasher.verify, password, password_hash)


def create_access_token(user_id: UUID, settings: Settings) -> str:
    issued_at = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, settings: Settings) -> UUID | None:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[JWT_ALGORITHM])
        return UUID(str(claims["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def set_auth_cookie(response: Response, token: str, settings: Settings) -> None:
    # HttpOnly keeps the token away from page scripts; Lax blocks cross-site POSTs.
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def clear_auth_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        COOKIE_NAME, httponly=True, samesite="lax", secure=settings.cookie_secure, path="/"
    )


def origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


class OriginGuardMiddleware:
    """Rejects state-changing requests whose Origin (or Referer) is not allow-listed."""

    def __init__(self, app: ASGIApp, allowed_origins: frozenset[str]) -> None:
        self.app = app
        self.allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        origin = request.headers.get("origin")
        if origin is None and (referer := request.headers.get("referer")):
            origin = origin_of(referer)
        if origin is None or origin.rstrip("/") not in self.allowed_origins:
            request_id = str(scope.get("state", {}).get("request_id", "req_unknown"))
            rejection = error_response(CsrfOriginRejected("Origin not allowed"), request_id)
            await rejection(scope, receive, send)
            return
        await self.app(scope, receive, send)
