"""Async Alembic environment; reuses the app's URL normalisation so SSL/pooler rules match."""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.db import build_engine
from app.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url = config.get_main_option("sqlalchemy.url") or get_settings().database_url
    engine = build_engine(url, use_null_pool=True)
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


asyncio.run(run_async_migrations())
