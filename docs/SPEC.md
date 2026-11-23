# EventSeat — Technical Specification

## 1. Vision & Core Guarantees

`EventSeat` is a concurrency-safe event and seat reservation engine. **PostgreSQL is the single source of truth (zero Redis).** It proves three guarantees under simultaneous contention:

- **Zero double-bookings** — three independent layers: serialized row locks (`SELECT … FOR UPDATE`), a partial unique index (`WHERE is_active = TRUE`), and composite foreign keys forcing `booking.event_id == booking_seat.event_id == seat.event_id`.
- **Zero ghost locks** — expired 5-minute holds are ignored by reads immediately (SQL clock), expired atomically inside booking transactions, and swept by a non-critical `SKIP LOCKED` housekeeper.
- **Zero duplicate retries, zero deadlocks** — domain-separated 64-bit advisory locks, savepoint-isolated idempotency records, and a strict 5-level global lock hierarchy.

## 2. Users & RBAC

| Role | Capabilities | Rules |
|---|---|---|
| `ATTENDEE` | Browse/search events, view seat maps, hold 1–4 unique seats (5 min), simulated checkout, view tickets, cancel own upcoming bookings, run concurrency demo | `403` on creating/cancelling events or touching another user's booking |
| `ORGANIZER` | All attendee actions + create events with seat grids, analytics + attendee roster for **own** events, cancel **own** events | Public signup ⇒ `is_verified_organizer = FALSE`. Unverified ⇒ `403 ORGANIZER_NOT_VERIFIED` on event creation. No access to other organizers' events (`403`) |
| `SUPER_ADMIN` | All capabilities + list/verify/revoke organizers, view/cancel any event or booking | Never creatable via `/auth/register`; seed/admin tooling only |

## 3. User Journeys

1. **Discover & log in (`/`)** — paginated, searchable, category-filtered catalog with live `available / total` seats. Email+password login, or 1-click Demo Login (Attendee / Verified Organizer / Super Admin). Auth = `HttpOnly; SameSite=Lax` cookie, same-origin.
2. **Provision (`/admin`)** — Super Admin verifies an Organizer; verified Organizer creates an event (`rows × cols`, max `10 × 12`, `booking_closes_at <= starts_at`, `starts_at` in the future) atomically with its seats.
3. **Hold (`/events/:id`)** — pick 1–4 `AVAILABLE` seats → **Hold Seats**. Frontend sends a UUIDv4 `Idempotency-Key`. Server enforces cutoff, hoarding caps, and returns `remaining_seconds`.
4. **Checkout** — monotonic countdown (`performance.now()` anchored to server `remaining_seconds`, re-synced on window focus) and a clearly labelled **Simulated Payment Panel** (`Success` / `Failure` / `Simulate Duplicate Retry`). Late confirm ⇒ `410` + seats released. Simulated failure ⇒ `402` (hold stays active; a new key may retry). Success ⇒ `CONFIRMED` + unique `ticket_code`.
5. **Tickets (`/my-bookings`)** — confirmed passes, active holds (resume/cancel), history. Cancel before `starts_at` frees seats instantly; history is preserved.
6. **Live concurrency proof** — **Simulate 20 Concurrent Requests** on a chosen free seat: exact live result `1 × 201`, `19 × 409`, `0` double-bookings, `active_seat_rows == 1`.

## 4. In Scope / Non-Goals

**In scope:** JWT-cookie auth + argon2 + Origin CSRF guard; 3-tier RBAC + demo login + idempotent seeding; catalog with live counts; organizer create/analytics/roster/cancel-event; multi-seat atomic hold; simulated checkout with idempotency + `410`; cancel & instant re-book; 60 s sweeper; 20-way concurrency simulator; same-origin multi-stage Docker + GitHub Actions CI; docs (`README`, `ARCHITECTURE`, `DECISIONS`, `LOCAL_VERIFICATION`, `runbook`).

**Out of scope (v1):** Redis (locks or cache); live payment gateway/webhooks (ADR-003 documents the extension); refresh-token tables; WebSockets, email/SMS, pricing tiers, waitlists, social login; `localStorage` tokens; rate limiting and login lockout (deferred to post-v1); frontend unit/E2E tests (covered manually via `docs/LOCAL_VERIFICATION.md`).

## 5. Stack, Runtime & Dependencies

- **Same-origin delivery.** Dev: Vite proxies `/api` → `http://localhost:8000`. Prod: FastAPI serves `frontend/dist` at `/` plus `/api/v1/*` and `/healthz` from one origin, so `SameSite=Lax` cookies always work.
- **DB.** PostgreSQL 16/17; `app/db.py` normalises `postgres://`/`postgresql://` URLs, maps `sslmode`/`channel_binding` to asyncpg SSL, and supports `USE_NULL_POOL=true` with `statement_cache_size=0` for pooled/serverless Postgres. Alembic reuses the same normalisation helper.
- **Env (`.env.example`):** `DATABASE_URL`, `TEST_DATABASE_URL`, `JWT_SECRET`, `JWT_EXPIRE_MINUTES=60`, `COOKIE_SECURE=false`, `ALLOWED_ORIGINS=http://localhost:5173,http://localhost:8000`, `USE_NULL_POOL=false`, `ENABLE_DEMO_TOOLS=true`, `RUN_SEED_ON_STARTUP=true`, `HOLD_TTL_SECONDS=300`, `SWEEPER_INTERVAL_SECONDS=60`.
- **Backend deps:** fastapi, uvicorn[standard], pydantic, pydantic-settings, sqlalchemy[asyncio], asyncpg, alembic, pyjwt, pwdlib[argon2], structlog; dev: pytest, pytest-asyncio, httpx, ruff, mypy.
- **Frontend deps:** react, react-dom, react-router-dom, @tanstack/react-query, tailwindcss (+ postcss/autoprefixer), shadcn primitives (`button card input dialog tabs badge table sonner label select`), lucide-react, clsx, tailwind-merge, class-variance-authority; dev: typescript, vite, eslint (+ react plugins), prettier.

## 6. Data Model (6 tables, Alembic `0001_initial_schema.py`)

```sql
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL UNIQUE,
    full_name VARCHAR(120) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(20) NOT NULL CHECK (role IN ('ATTENDEE','ORGANIZER','SUPER_ADMIN')),
    is_verified_organizer BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_users_email_lowercase CHECK (email = LOWER(email))
);

CREATE TABLE events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organizer_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    title VARCHAR(160) NOT NULL,
    category VARCHAR(40) NOT NULL CHECK (category IN ('CONFERENCE','WORKSHOP','MEETUP','SEMINAR')),
    venue VARCHAR(200) NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    booking_closes_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PUBLISHED' CHECK (status IN ('PUBLISHED','CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_events_cutoff_before_start CHECK (booking_closes_at <= starts_at)
);
CREATE INDEX ix_events_status_starts ON events (status, starts_at);
CREATE INDEX ix_events_organizer ON events (organizer_id);

CREATE TABLE seats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
    seat_label VARCHAR(16) NOT NULL,
    row_label VARCHAR(8) NOT NULL,
    seat_number INTEGER NOT NULL CHECK (seat_number > 0),
    CONSTRAINT uq_seats_event_label UNIQUE (event_id, seat_label),
    CONSTRAINT uq_seats_id_event UNIQUE (id, event_id)
);

CREATE TABLE bookings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    event_id UUID NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
    status VARCHAR(20) NOT NULL CHECK (status IN ('HELD','CONFIRMED','CANCELLED','EXPIRED')),
    held_until TIMESTAMPTZ,
    ticket_code VARCHAR(24) UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    CONSTRAINT uq_bookings_id_event UNIQUE (id, event_id),
    CONSTRAINT ck_bookings_held_has_expiry CHECK (status <> 'HELD' OR held_until IS NOT NULL),
    CONSTRAINT ck_bookings_confirmed_fields CHECK (status <> 'CONFIRMED' OR (ticket_code IS NOT NULL AND confirmed_at IS NOT NULL)),
    CONSTRAINT ck_bookings_cancelled_fields CHECK (status <> 'CANCELLED' OR cancelled_at IS NOT NULL)
);
CREATE INDEX ix_bookings_user_created ON bookings (user_id, created_at DESC);
CREATE INDEX ix_bookings_event_status ON bookings (event_id, status);
CREATE INDEX ix_bookings_held_expiry ON bookings (held_until) WHERE status = 'HELD';
CREATE UNIQUE INDEX uq_bookings_one_active_hold_per_user_event
    ON bookings (user_id, event_id) WHERE status = 'HELD';

CREATE TABLE booking_seats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id UUID NOT NULL,
    event_id UUID NOT NULL,
    seat_id UUID NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT fk_booking_seats_booking_event
        FOREIGN KEY (booking_id, event_id) REFERENCES bookings(id, event_id) ON DELETE RESTRICT,
    CONSTRAINT fk_booking_seats_seat_event
        FOREIGN KEY (seat_id, event_id) REFERENCES seats(id, event_id) ON DELETE RESTRICT,
    CONSTRAINT uq_booking_seats_booking_seat UNIQUE (booking_id, seat_id)
);
CREATE INDEX ix_booking_seats_booking ON booking_seats (booking_id);
CREATE UNIQUE INDEX uq_booking_seats_active_seat
    ON booking_seats (event_id, seat_id) WHERE is_active = TRUE;

CREATE TABLE idempotency_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    endpoint VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    request_hash CHAR(64) NOT NULL,
    status_code INTEGER NOT NULL CHECK (status_code >= 200 AND status_code < 500),
    response_body JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '24 hours'),
    CONSTRAINT uq_idempotency_user_endpoint_key UNIQUE (user_id, endpoint, idempotency_key)
);
CREATE INDEX ix_idempotency_expires ON idempotency_records (expires_at);
```

DDL `DEFAULT NOW()` on audit columns is fine (row-creation time). The **decision logic** clock rule is §7.2.
`idempotency_records.endpoint` values: `"hold"` and `"confirm:{booking_id}"`.

## 7. Concurrency Architecture

### 7.1 Global lock hierarchy
`L1` idempotency advisory → `L2` hoarding advisory → `L3` event row → `L4` seat rows (id ASC) → `L5` booking rows (id ASC). Ascending only; skipping allowed; going backwards forbidden. Rationale to document in ADR-006: advisory locks serialize *logical* actors before touching rows; seat rows are the mutual-exclusion primitive (they exist before any booking does); the partial unique index and composite FKs are defence in depth, proven independently by T8.

### 7.2 Clock rule (see ADR-005)
`NOW()` returns *transaction start* time and goes stale after waiting on an advisory/row lock; a confirm that queued 2 s behind another request would be judged against a clock 2 s old. All decision comparisons therefore use:

```python
def sql_now() -> ColumnElement[datetime]:
    return func.statement_timestamp()   # fresh per statement, stable within one statement
```

`held_until = sql_now() + timedelta(seconds=HOLD_TTL_SECONDS)`. `remaining_seconds = GREATEST(0, CEIL(EXTRACT(EPOCH FROM (held_until - sql_now()))))`, computed in SQL. Event-creation checks (`starts_at > now`) also use a `SELECT` of `sql_now()`, never Python time.

### 7.3 Helpers (`app/db.py`, `app/services/idempotency.py`)

```python
def advisory_lock_key(domain: Literal["idem", "hoard"], *parts: str) -> int:
    raw = f"{domain}:{':'.join(parts)}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], byteorder="big", signed=True)

def hold_request_hash(event_id: UUID, seat_ids: list[UUID]) -> str:
    canonical = f"{event_id}:{','.join(sorted(str(s) for s in seat_ids))}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def confirm_request_hash(booking_id: UUID, simulate_outcome: str) -> str:
    return hashlib.sha256(f"{booking_id}:{simulate_outcome}".encode("utf-8")).hexdigest()
```

### 7.4 Transaction matrix

| Operation | Pre-check → lock order | State transitions & savepoints |
|---|---|---|
| `POST /bookings/hold` | Validate key (1–128 chars) + 1..4 unique `seat_ids` → `L1 idem:{uid}:hold:{key}` → prune record if `expires_at <= sql_now()` → replay / mismatch check → `L2 hoard:{uid}:{event_id}` → `L3 events FOR SHARE` (missing ⇒ `404 EVENT_NOT_FOUND`) → `L4 requested seats ORDER BY id FOR UPDATE` → `L5 conflicting bookings ORDER BY id FOR UPDATE` | **Outer tx:** lazy-expire every `HELD` booking with `held_until <= sql_now()` that touches the requested seats or belongs to `(uid, event_id)` (`status='EXPIRED'`, `is_active=FALSE`). **Savepoint**, validate in this order: event `PUBLISHED` (`409 EVENT_NOT_PUBLISHED`) → `sql_now() < booking_closes_at` (`409 BOOKING_CLOSED`) → all seats exist for `event_id` (`404 SEAT_NOT_FOUND`) → seats free (`409 SEAT_UNAVAILABLE`) → caps: ≤ 1 active hold and ≤ 4 active seats (HELD+CONFIRMED) for user/event (`409 USER_LIMIT_EXCEEDED`) → insert booking + booking_seats. Deterministic `409`: roll back savepoint, persist `409`, commit. `5xx`: full rollback, persist nothing. |
| `POST /bookings/{id}/confirm` | Validate key → unlocked read `(owner_id, event_id, seat_ids)` → `404` if missing → `403` if `owner_id != me` **before locking** → `L1 idem:{uid}:confirm:{id}:{key}` → prune/replay/mismatch → `L2 hoard:{owner}:{event}` → `L3 FOR SHARE` → `L4 booking's seats FOR UPDATE` → `L5 booking FOR UPDATE` | Re-verify ownership + event `PUBLISHED` (`409 EVENT_NOT_PUBLISHED`). Then by state: `CONFIRMED` → persist `409 BOOKING_ALREADY_CONFIRMED`; `CANCELLED` → `409 BOOKING_CANCELLED`; `EXPIRED` → `410 BOOKING_HOLD_EXPIRED` (all three: roll back savepoint, persist, commit). `HELD` & expired → **set `EXPIRED` + `is_active=FALSE` + persist `410` + commit (no rollback)**. `HELD` & live & `FAILURE` → persist `402 PAYMENT_SIMULATION_FAILED` (hold stays active). `HELD` & live & `SUCCESS` → `CONFIRMED`, `ticket_code`, `confirmed_at`, persist `200`, commit. |
| `POST /bookings/{id}/cancel` | Unlocked read → `404` → `403` (owner or `SUPER_ADMIN`) before locking → `L2 hoard:{owner}:{event}` → `L3 FOR SHARE` → `L4 seats` → `L5 booking` | Re-verify authz. `CANCELLED` ⇒ `200` (naturally idempotent). `EXPIRED` ⇒ `409 BOOKING_ALREADY_EXPIRED`. `HELD` & expired ⇒ commit `EXPIRED` + `is_active=FALSE`, return `409 BOOKING_ALREADY_EXPIRED`. `sql_now() >= starts_at` ⇒ `409 EVENT_ALREADY_STARTED`. Else `CANCELLED`, `cancelled_at`, `is_active=FALSE`, commit. |
| `POST /events/{id}/cancel` | Unlocked read `organizer_id` → `404` → `403` (owner or `SUPER_ADMIN`) → `L3 events FOR UPDATE` → `L4 all event seats` → `L5 all HELD/CONFIRMED bookings` | Already `CANCELLED` ⇒ `200`. Else event `CANCELLED`; all `HELD`/`CONFIRMED` bookings ⇒ `CANCELLED` (`cancelled_at`); `is_active=FALSE`; commit. |
| Sweeper (every `SWEEPER_INTERVAL_SECONDS`) | `L5` only: `SELECT id FROM bookings WHERE status='HELD' AND held_until <= sql_now() ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 100` | Expire matched bookings + their `booking_seats`; delete ≤ 500 `idempotency_records WHERE expires_at <= sql_now()`; commit. Never `L1–L4`. Failures are logged and the loop continues. |

### 7.5 Idempotency semantics
- Persisted: `2xx`, `402`, `409`, `410`. Not persisted: `403`, `404`, `422`, `5xx`, and `IDEMPOTENCY_MISMATCH` itself.
- Same `(user, endpoint, key)` + same hash ⇒ replay stored status/body with header `Idempotent-Replayed: true`. Stored `remaining_seconds` is as-of the original response; clients re-sync via `GET /bookings/me`.
- Same key + different hash ⇒ `409 IDEMPOTENCY_MISMATCH`.
- Seat order in the payload never changes the hash (`[B, A]` ≡ `[A, B]`).
- Concurrent identical requests: the second blocks on `L1`, then replays the first's committed result.

### 7.6 Sweeper
Single `asyncio` task started in `lifespan`, cancelled and awaited on shutdown. Exposes `run_sweeper_once(session_factory) -> SweepResult` so tests call it directly. Correctness never depends on it (reads and bookings lazily expire).

### 7.7 Concurrency simulator (`POST /events/{id}/simulate-concurrency`)
Requires auth + `ENABLE_DEMO_TOOLS=true` (else `404`). Takes `{seat_id}`. Single-flight `asyncio.Lock` ⇒ `429 DEMO_BUSY`. Opens 20 independent sessions, one per seeded `race-demo-NN@eventseat.demo` account, and runs `hold_seats()` concurrently via `asyncio.gather`. Response: counts per status code, `double_bookings`, `active_seat_rows`, `duration_ms`, `released_holds`. **In a `finally`, the winning demo hold is cancelled/expired so the seat returns to `AVAILABLE`** and demo accounts never accumulate holds. The lock is per-process; documented as a single-instance limitation (ADR-002).

## 8. API Contract (18 endpoints)

**Error envelope (all 4xx/5xx):** `{"error": {"code": "SEAT_UNAVAILABLE", "message": "...", "request_id": "req_8f9a2c1d"}}`. `request_id` also in `X-Request-ID`. Unhandled exceptions ⇒ `500 INTERNAL_ERROR`, logged with traceback, no internals leaked.

**Error codes:** `401 UNAUTHENTICATED` · `403 FORBIDDEN, ORGANIZER_NOT_VERIFIED, CSRF_ORIGIN_REJECTED` · `404 NOT_FOUND, EVENT_NOT_FOUND, SEAT_NOT_FOUND, BOOKING_NOT_FOUND` · `409 SEAT_UNAVAILABLE, USER_LIMIT_EXCEEDED, IDEMPOTENCY_MISMATCH, BOOKING_CLOSED, EVENT_NOT_PUBLISHED, BOOKING_ALREADY_CONFIRMED, BOOKING_CANCELLED, BOOKING_ALREADY_EXPIRED, EVENT_ALREADY_STARTED, EMAIL_ALREADY_REGISTERED` · `402 PAYMENT_SIMULATION_FAILED` · `410 BOOKING_HOLD_EXPIRED` · `422 VALIDATION_ERROR` · `429 DEMO_BUSY` · `500 INTERNAL_ERROR`.

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/healthz` | Public | `SELECT 1` liveness |
| POST | `/api/v1/auth/register` | Public | `role ∈ {ATTENDEE, ORGANIZER}`, password 8–128, lowercase email, sets cookie |
| POST | `/api/v1/auth/login` | Public | argon2 verify off-loop; dummy-hash on unknown email |
| POST | `/api/v1/auth/demo-login` | Public | `ENABLE_DEMO_TOOLS=true`, else `404` |
| POST | `/api/v1/auth/logout` | Auth | Clears cookie |
| GET | `/api/v1/auth/me` | Auth | Profile + role |
| GET | `/api/v1/events` | Public | `q`, `category`, `page=1`, `page_size=12` (max 50); live counts; one query (window `count(*)`) |
| POST | `/api/v1/events` | Verified ORGANIZER / SUPER_ADMIN | Event + `rows × cols` seats (≤ `10×12`) atomically; row labels `A…J`, seat labels `A1…` |
| GET | `/api/v1/events/{id}/seats` | Public (optional auth) | One query ⇒ `AVAILABLE / HELD / BOOKED / MINE` (`MINE` = my live hold or confirmed seat) |
| GET | `/api/v1/events/{id}/analytics` | Event ORGANIZER / SUPER_ADMIN | `available, held, confirmed, utilization_pct` (1 dp) + confirmed roster |
| POST | `/api/v1/events/{id}/cancel` | Event ORGANIZER / SUPER_ADMIN | §7.4 |
| POST | `/api/v1/events/{id}/simulate-concurrency` | Auth + demo flag | §7.7 |
| POST | `/api/v1/bookings/hold` | Auth | **`Idempotency-Key` required.** `{event_id, seat_ids}` ⇒ `201` + `remaining_seconds` |
| POST | `/api/v1/bookings/{id}/confirm` | Owner | **`Idempotency-Key` required.** `{simulate_outcome: "SUCCESS"\|"FAILURE"}` ⇒ `200/402/409/410` |
| POST | `/api/v1/bookings/{id}/cancel` | Owner / SUPER_ADMIN | §7.4 |
| GET | `/api/v1/bookings/me` | Auth | Tickets, live holds (`remaining_seconds`), history; `limit` ≤ 100 |
| GET | `/api/v1/admin/organizers` | SUPER_ADMIN | Organizers + verification flag |
| PATCH | `/api/v1/admin/organizers/{user_id}/verify` | SUPER_ADMIN | `{is_verified: bool}` |

Missing `Idempotency-Key` on hold/confirm ⇒ `422 VALIDATION_ERROR`.
CSRF: on `POST/PUT/PATCH/DELETE`, `Origin` (else `Referer`'s origin) must exactly match `ALLOWED_ORIGINS`; otherwise `403 CSRF_ORIGIN_REJECTED`.

## 9. Frontend Screens (4)

1. **`/` Catalog & Auth** — search, category pills, paginated cards (venue, cutoff countdown, `available/total`), Navbar with login/register modal + Demo Login bar.
2. **`/events/:id` Seat map, checkout, simulator** — colour-coded grid (`AVAILABLE` green, `HELD` amber, `BOOKED` slate, `MINE` blue), multi-select 1–4, checkout panel (monotonic countdown, labelled simulated payment, duplicate-retry reusing the same key), collapsible 20-request simulator.
3. **`/my-bookings`** — tickets, active holds (resume/cancel), history.
4. **`/admin`** — role-gated tabs: Create Event (+ seat-grid builder) · My Events analytics/roster/cancel · Organizer verification toggle.

Every data view has explicit loading, empty and error states. API errors surface `error.message` and `request_id`.

## 10. Acceptance Tests (real PostgreSQL, never SQLite)

Tests run against a migrated database (Alembic), truncated between tests; concurrency tests use a pool ≥ 25.

- **T1 Race** (`test_concurrency_idempotency`): 20 users hold the same seat concurrently ⇒ exactly `1×201`, `19×409`, and `COUNT(*) booking_seats WHERE seat_id=:s AND is_active` = 1.
- **T2 Deadlock freedom**: A `[A1,A2]` vs B `[A2,A1]`, 10 trials ⇒ no `40P01`, one winner, one clean `409` each trial.
- **T3 Idempotency**: concurrent identical `(user,key,payload)` ⇒ same booking id, one row; same key + different seat ⇒ `409 IDEMPOTENCY_MISMATCH`; reversed seat order replays `201`; `409 SEAT_UNAVAILABLE` and `402` persist and replay; new key after `402` succeeds; expired record is pruned and re-executed; replay carries `Idempotent-Replayed: true`.
- **T4 Hoarding cap**: same user, two simultaneous holds on distinct seats ⇒ `1×201`, `1×409 USER_LIMIT_EXCEEDED`; 3 confirmed + hold of 2 ⇒ `409`.
- **T5 Expiry** (`test_bookings_expiry`): set `held_until = now - 1s` ⇒ seat map shows `AVAILABLE` immediately; `/confirm` ⇒ `410`, `EXPIRED` + `is_active=FALSE` committed; user B's `/hold` succeeds via lazy expiry without the sweeper.
- **T6 Cancel & re-book**: A holds→confirms→cancels; B holds+confirms same seat; both rows kept; cancelling an expired hold ⇒ `409` with `EXPIRED` committed; cancel after `starts_at` ⇒ `409`.
- **T7 Security & RBAC** (`test_auth_events`): `role="SUPER_ADMIN"` on register ⇒ `422`; unverified organizer create ⇒ `403`; admin verifies ⇒ `201`; confirm/cancel another user's booking ⇒ `403` **without acquiring locks**; duplicate `seat_ids` ⇒ `422`; seat from event B with event A's id ⇒ `404 SEAT_NOT_FOUND`; foreign/missing `Origin` ⇒ `403`; hold after `booking_closes_at` ⇒ `409`; cookie is `HttpOnly; SameSite=Lax`; uppercase email normalised; dev `JWT_SECRET` rejected when `COOKIE_SECURE=true`.
- **T8 DB-level guards**: bypassing the service, raw inserts violating `uq_booking_seats_active_seat`, the cross-event composite FKs, the `ck_*` constraints and `status_code >= 500` are each rejected by PostgreSQL.
- **T9 Sweeper**: `run_sweeper_once` expires stale holds and prunes old idempotency rows; skips a booking locked by another transaction (`SKIP LOCKED`).
- **T10 Event cancel**: releases all seats, bookings ⇒ `CANCELLED`, second call ⇒ `200`; concurrent hold vs event-cancel ⇒ no deadlock, no active seat left on a cancelled event.
- **T11 Simulator**: result shape `1×201 / 19×409`, winning hold released, second concurrent call ⇒ `429`, demo disabled ⇒ `404`.
- **T12 Reads**: catalog search/category/pagination/live counts; seat map statuses incl. `MINE`; analytics numbers; organizer cannot read another organizer's analytics (`403`).
- **T0 Foundation**: `/healthz`, request-id echo, error envelope shape, `DATABASE_URL` normalisation table.

## 11. Repository Structure

```text
eventseat/
├── README.md  .gitignore  .env.example  .dockerignore
├── docker-compose.yml  Dockerfile  .github/workflows/ci.yml
├── docs/
│   └── SPEC.md  ARCHITECTURE.md  DECISIONS.md  LOCAL_VERIFICATION.md  runbook.md
├── backend/
│   ├── pyproject.toml  alembic.ini
│   ├── alembic/  env.py · versions/0001_initial_schema.py
│   ├── app/
│   │   ├── main.py  config.py  db.py  errors.py  observability.py  security.py
│   │   ├── models.py  schemas.py  seed.py
│   │   ├── services/  auth_service.py  event_service.py  queries.py  idempotency.py
│   │   │              expiry.py  booking_hold_service.py  booking_lifecycle_service.py
│   │   │              concurrency_demo.py
│   │   └── api/  deps.py  auth.py  events.py  bookings.py  admin.py
│   └── tests/  conftest.py  test_auth_events.py  test_bookings_expiry.py
│               test_concurrency_idempotency.py
└── frontend/
    ├── package.json  tsconfig.json  vite.config.ts  tailwind.config.ts
    └── src/
        ├── main.tsx  App.tsx
        ├── lib/  api.ts  types.ts  hooks.ts
        ├── components/  ui/*  Navbar.tsx  SeatGrid.tsx  CheckoutPanel.tsx  ConcurrencyPanel.tsx
        └── pages/  EventsPage.tsx  EventDetailPage.tsx  MyBookingsPage.tsx  AdminPage.tsx
