# Architecture

EventSeat is a modular monolith: one FastAPI process, one PostgreSQL database, one React SPA served from the same origin.

```
Browser ── same origin ──▶ FastAPI ──▶ PostgreSQL 16
  SPA (React)              api/  thin routers, validation, auth
  HttpOnly cookie          services/  all business rules and locking
                           models/db  SQLAlchemy models, sql_now(), advisory locks
                           sweeper    background task started in lifespan
```

## Backend layout

| Module | Responsibility |
| --- | --- |
| `api/` | Routers (`auth`, `events`, `bookings`, `admin`) and dependencies (`CurrentUser`, `IdempotencyKey`, `DbSession`). No business logic. |
| `services/booking_hold_service.py` | Hold flow and the shared lock helpers (`acquire_hoard_lock`, `lock_event`, `lock_seats`). |
| `services/booking_lifecycle_service.py` | Confirm and cancel. Unlocked read, then 404, then 403, then locks, then re-validation. |
| `services/idempotency.py` | Prune, replay, mismatch and result storage under the L1 advisory lock. |
| `services/expiry.py` | Lazy expiry, `expire_stale_holds`, `run_sweeper_once`, `sweeper_loop`. |
| `services/queries.py` | Catalog, seat map, analytics, my bookings, and the single live-occupancy predicate. |
| `services/concurrency_demo.py` | The 20-way race simulator. |
| `db.py` | URL normalisation, engine and pool setup, `sql_now()`, advisory-lock key derivation. |
| `errors.py` | Typed errors, one JSON envelope, integrity-error mapping. |
| `security.py`, `observability.py` | JWT cookie and argon2 hashing; request-id / `X-Demo-Tools` middleware and logging. |

Migrations run before the server starts (`alembic upgrade head`), not inside the app lifespan.

## Data model

Six tables: `users`, `events`, `seats`, `bookings`, `booking_seats`, `idempotency_records`. The invariant "a seat has at most one live claim" is enforced in the database by `uq_booking_seats_active_seat`, a unique index on `(event_id, seat_id)` where `is_active`. Composite foreign keys `(booking_id, event_id)` and `(seat_id, event_id)` make it impossible to attach a seat from one event to a booking for another. Check constraints require `held_until` on HELD rows, ticket and confirmation fields on CONFIRMED rows, and `cancelled_at` on CANCELLED rows. A seat's live occupancy is defined once, in `queries.seat_occupied_predicate()`: an active `booking_seats` row whose booking is CONFIRMED, or HELD with `held_until > sql_now()`.

## Lock hierarchy

Within one transaction locks are taken in ascending order only; skipping a level is allowed, going back is not.

| Level | Lock | Taken by |
| --- | --- | --- |
| L1 | `pg_advisory_xact_lock` on `idem:{user}:{endpoint}:{key}` | hold, confirm |
| L2 | `pg_advisory_xact_lock` on `hoard:{user}:{event}` | hold |
| L3 | `events` row: `FOR SHARE` (hold, confirm, cancel-booking), `FOR UPDATE` (cancel-event) | all booking paths |
| L4 | `seats` rows `ORDER BY id FOR UPDATE` | hold, cancel-event |
| L5 | `bookings` rows `ORDER BY id FOR UPDATE` | confirm, cancel, sweeper |

Advisory keys are the first 8 bytes of a domain-prefixed SHA-256, read as a signed 64-bit integer and bound as `BIGINT`. The sweeper takes L5 only and uses `SKIP LOCKED`, so it never waits on a request. See ADR-006.

## Request flows

**Hold.** Validate input, then L1 (replay or mismatch check), L2 (per-user serialisation), L3 `FOR SHARE`, L4 lock the requested seats in id order, expire stale holds on those seats, check the occupancy predicate and the 4-seat cap, insert the booking and `booking_seats`, store the idempotent result.

**Confirm.** Read the booking without locks, return 404 if missing and 403 if it belongs to someone else, then take L1, L3, L5 and re-validate. The `UPDATE` re-checks `held_until > sql_now()`. If the hold has expired it is marked EXPIRED, a `410` is persisted for that key and committed.

**Expiry.** Reads treat an expired HELD booking as free immediately. The sweeper converts them to EXPIRED in batches of 100 and prunes old idempotency rows (up to 500, also `SKIP LOCKED`).

## Idempotency

Hold and confirm require an `Idempotency-Key`. The request hash covers the canonical payload (seat ids sorted, so a reversed order replays). Deterministic outcomes (201, 402, 409, 410) are stored and replayed with `Idempotent-Replayed: true`; 5xx responses are never stored, enforced both in code (`is_persistable`) and by a CHECK on `status_code`. Expired-hold cleanup runs in the outer transaction before the savepoint, so a failed attempt cannot roll back the expiry.

## Authentication and CSRF

A JWT lives only in an HttpOnly, SameSite=Lax cookie; the SPA never sees it. A pure-ASGI `OriginGuardMiddleware` rejects state-changing requests unless `Origin` (or, failing that, `Referer`) is an allowed origin. Passwords use argon2 (via `pwdlib`, off the event loop); unknown emails still verify against a dummy hash. Request models forbid extra fields, so a client cannot self-assign a role.

## Frontend

React Router pages (`EventsPage`, `EventDetailPage`, `MyBookingsPage`, `AdminPage`) over TanStack Query. The SPA reads the `X-Demo-Tools` response header to hide demo login and the concurrency simulator when `ENABLE_DEMO_TOOLS=false`, clears cached queries and redirects to `/` on confirmed sign-out, and refetches the seat map every 5 s. The hold countdown is anchored to the server's `remaining_seconds` and advanced with `performance.now()`, so changing the device clock cannot extend a hold. Every data view has loading, empty and error states, and errors show the server's `request_id`. Checkout is a clearly labelled simulation; "Send the same request again" re-uses the Idempotency-Key to show a replay.

## Deployment shape

A multi-stage `Dockerfile` builds the SPA, installs runtime dependencies into a venv, and runs as a non-root user with a `/healthz` `HEALTHCHECK`. The container runs `alembic upgrade head` then uvicorn with proxy headers enabled. Pool settings: 10 connections plus 15 overflow by default, or `NullPool` with `statement_cache_size=0` for pooled/serverless Postgres.
