"""Idempotency primitives: the L1 lock, prune/replay/mismatch, persistence, request hashes."""

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import advisory_lock_key, advisory_xact_lock, sql_now
from app.errors import AppError, IdempotencyMismatch, error_body
from app.models import IdempotencyRecord

PERSISTABLE_ERROR_STATUSES = frozenset({402, 409, 410})
RECORD_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class IdempotencyScope:
    """Everything that identifies one idempotent request: who, which endpoint, key, payload."""

    user_id: UUID
    endpoint: str
    key: str
    request_hash: str


@dataclass(frozen=True)
class IdempotentResult:
    status_code: int
    body: dict[str, object]
    replayed: bool = False


def is_persistable(status_code: int) -> bool:
    """2xx and the deterministic 402/409/410 outcomes. 5xx is never stored."""
    return 200 <= status_code < 300 or status_code in PERSISTABLE_ERROR_STATUSES


def hold_request_hash(event_id: UUID, seat_ids: list[UUID]) -> str:
    canonical = f"{event_id}:{','.join(sorted(str(s) for s in seat_ids))}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def confirm_request_hash(booking_id: UUID, simulate_outcome: str) -> str:
    return hashlib.sha256(f"{booking_id}:{simulate_outcome}".encode()).hexdigest()


async def begin_idempotent_request(
    session: AsyncSession, scope: IdempotencyScope
) -> IdempotentResult | None:
    """Take L1, drop a stale record, and return the stored result if this is a replay."""
    lock_key = advisory_lock_key("idem", str(scope.user_id), scope.endpoint, scope.key)
    await advisory_xact_lock(session, lock_key)  # L1: idempotency advisory
    identity = (
        IdempotencyRecord.user_id == scope.user_id,
        IdempotencyRecord.endpoint == scope.endpoint,
        IdempotencyRecord.idempotency_key == scope.key,
    )
    await session.execute(
        delete(IdempotencyRecord).where(*identity, IdempotencyRecord.expires_at <= sql_now())
    )
    stored = (
        await session.execute(
            select(
                IdempotencyRecord.request_hash,
                IdempotencyRecord.status_code,
                IdempotencyRecord.response_body,
            ).where(*identity)
        )
    ).one_or_none()
    if stored is None:
        return None
    if stored.request_hash != scope.request_hash:
        raise IdempotencyMismatch("Idempotency-Key was already used with a different request")
    return IdempotentResult(stored.status_code, stored.response_body, replayed=True)


async def store_result(
    session: AsyncSession, scope: IdempotencyScope, status_code: int, body: dict[str, object]
) -> IdempotentResult:
    if not is_persistable(status_code):
        raise ValueError(f"status {status_code} must never be persisted")
    await session.execute(
        insert(IdempotencyRecord).values(
            user_id=scope.user_id,
            endpoint=scope.endpoint,
            idempotency_key=scope.key,
            request_hash=scope.request_hash,
            status_code=status_code,
            response_body=body,
            expires_at=sql_now() + RECORD_TTL,
        )
    )
    return IdempotentResult(status_code, body)


async def store_error(
    session: AsyncSession, scope: IdempotencyScope, error: AppError, request_id: str
) -> IdempotentResult:
    return await store_result(session, scope, error.status_code, error_body(error, request_id))
