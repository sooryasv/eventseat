"""Engine and session factory, the SQL clock, and advisory-lock helpers."""

import hashlib
from datetime import datetime
from typing import Literal

from sqlalchemy import BigInteger, DateTime, func, literal, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.elements import ColumnElement

SSL_REQUIRED_MODES = frozenset({"require", "verify-ca", "verify-full"})
# asyncpg rejects both of these as URL query parameters, so they are stripped and mapped.
UNSUPPORTED_QUERY_KEYS = ("sslmode", "channel_binding")


def normalise_database_url(raw_url: str) -> tuple[str, bool]:
    """Return (postgresql+asyncpg URL without libpq-only params, whether SSL is required)."""
    url = make_url(raw_url)
    query = dict(url.query)
    sslmode = query.get("sslmode")
    ssl_required = isinstance(sslmode, str) and sslmode in SSL_REQUIRED_MODES
    for key in UNSUPPORTED_QUERY_KEYS:
        query.pop(key, None)
    normalised = url.set(drivername="postgresql+asyncpg", query=query)
    return normalised.render_as_string(hide_password=False), ssl_required


def build_engine(raw_url: str, *, use_null_pool: bool) -> AsyncEngine:
    url, ssl_required = normalise_database_url(raw_url)
    # statement_cache_size=0 keeps asyncpg compatible with PgBouncer-style poolers (Neon, Supabase).
    connect_args: dict[str, object] = {"statement_cache_size": 0}
    if ssl_required:
        connect_args["ssl"] = "require"
    if use_null_pool:
        return create_async_engine(url, poolclass=NullPool, connect_args=connect_args)
    return create_async_engine(
        url, pool_size=10, max_overflow=15, pool_pre_ping=True, connect_args=connect_args
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def sql_now() -> ColumnElement[datetime]:
    """The only clock for decision logic.

    NOW() is frozen at transaction start and goes stale after waiting on a lock (ADR-005);
    statement_timestamp() is fresh for every statement.
    """
    return func.statement_timestamp(type_=DateTime(timezone=True))


def advisory_lock_key(domain: Literal["idem", "hoard"], *parts: str) -> int:
    """Domain-separated signed 64-bit key, so unrelated lock families can never collide."""
    raw = f"{domain}:{':'.join(parts)}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], byteorder="big", signed=True)


async def advisory_xact_lock(session: AsyncSession, key: int) -> None:
    # BigInteger is explicit: asyncpg would otherwise bind the key as int4 and overflow.
    await session.execute(select(func.pg_advisory_xact_lock(literal(key, BigInteger))))
