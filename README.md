# EventSeat

A concurrency-safe event and seat reservation engine. Many people can try to hold the same seat at the same instant; PostgreSQL guarantees exactly one wins, holds expire on a SQL clock, and retried requests never double-book.

**Stack:** FastAPI, async SQLAlchemy 2, asyncpg, PostgreSQL 16 (only), Alembic, React 18 + TypeScript (strict), Vite, Tailwind, TanStack Query. One Docker image serves the API and the built SPA from the same origin.

## What it demonstrates

- **Race-free seat holds.** A strict lock hierarchy (L1 idempotency advisory, L2 per-user hoarding advisory, L3 event row, L4 seat rows by id, L5 booking rows by id) makes deadlocks structurally impossible. A partial unique index and composite foreign keys back it up at the database level.
- **A SQL-only clock.** Every expiry decision uses `statement_timestamp()` through one helper, `sql_now()`. `NOW()` is never used because it goes stale after waiting on a lock.
- **Savepoint-safe idempotency.** `Idempotency-Key` on hold and confirm: replays return the stored response with `Idempotent-Replayed: true`; the same key with a different payload is `409`; server errors (5xx) are never stored.
- **Lazy plus background expiry.** A hold past `held_until` is treated as free immediately; a sweeper (`SKIP LOCKED`) tidies rows every 60 s.
- **A live concurrency demo.** The UI can fire 20 simultaneous holds at one seat and show `201 ×1, 409 ×19, double bookings 0`.

Design rationale lives in [docs/DECISIONS.md](docs/DECISIONS.md) (ADR-001..007) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## What is simulated, and what is out of scope

- **Payments are simulated.** The checkout step has "payment succeeds / fails" buttons, labelled as a demo. No card data is collected and nothing is charged. [ADR-003](docs/DECISIONS.md) describes the real-gateway extension.
- **The concurrency simulator is single-instance.** Its single-flight lock is an in-process `asyncio.Lock`, so with several app instances each would allow one run. Real booking correctness does not depend on it.
- **Not built (v1 non-goals):** Redis, refresh tokens, rate limiting, login lockout, WebSockets, email/SMS, pricing, waitlists, social login, frontend unit/E2E tests. See `docs/SPEC.md` §4.

## Run it locally

Prerequisites: Docker, Python 3.12, Node 20.

```bash
cp .env.example .env
docker compose up -d db                 # Postgres 16 on :5432

cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:create_app --factory --reload      # API on :8000

# second terminal
cd frontend && npm ci && npm run dev                # UI on :5173 (proxies /api to :8000)
```

On startup the API seeds three events and the demo accounts (`attendee@eventseat.demo`, organizer and admin personas, and 20 `race-demo-NN@eventseat.demo` users). The navbar's "Demo login" buttons use them.

Everything in one container (API + built UI on :8000):

```bash
docker compose --profile full up --build
```

Configuration is environment-driven; see `.env.example`. With `COOKIE_SECURE=true` the app refuses to start on the default dev `JWT_SECRET`. For pooled or serverless Postgres set `USE_NULL_POOL=true`; `sslmode`/`channel_binding` URL parameters are normalised for asyncpg.

## Quality gates

```bash
cd backend
ruff check . && ruff format --check .
mypy app tests                       # strict, configured in pyproject.toml
docker compose up -d db-test         # Postgres 16 on :5433
TEST_DATABASE_URL=postgresql://eventseat:eventseat@localhost:5433/eventseat_test pytest

cd ../frontend
npx tsc --noEmit && npm run lint && npm run build
```

The 76 backend tests (suites T0–T12) run against a real Alembic-migrated PostgreSQL, never SQLite or mocks. They cover the 20-way race, deadlock freedom, idempotency, hoarding cap, expiry, cancel and re-book, RBAC, database-level constraint guards, the sweeper, event cancellation, the simulator and read endpoints.

Measured locally on PostgreSQL 16 (not a load benchmark): all 76 tests pass in ~12–24 s, and the simulator's 20-way race returns `201 ×1, 409 ×19, double bookings 0`. See the [local verification guide](docs/LOCAL_VERIFICATION.md) for a manual walkthrough to reproduce every guarantee yourself.

## API (all under `/api/v1`)

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/auth/register`, `/auth/login`, `/auth/logout` | HttpOnly cookie session; register accepts only ATTENDEE or ORGANIZER |
| POST | `/auth/demo-login` | Demo personas; `404` when `ENABLE_DEMO_TOOLS=false` |
| GET | `/auth/me` | Current user |
| GET | `/events` | Search, category filter, pagination, live availability |
| POST | `/events` | Verified organizer or admin |
| GET | `/events/{id}/seats` | Seat map with `AVAILABLE / HELD / BOOKED / MINE` |
| GET | `/events/{id}/analytics` | Organizer of the event or admin |
| POST | `/events/{id}/cancel` | Releases every hold and booking |
| POST | `/events/{id}/simulate-concurrency` | Demo tool, auth required |
| POST | `/bookings/hold` | Needs `Idempotency-Key`; 1–4 seats |
| POST | `/bookings/{id}/confirm` | Needs `Idempotency-Key`; `410` if the hold expired |
| POST | `/bookings/{id}/cancel`, GET `/bookings/me` | Own bookings |
| GET/PATCH | `/admin/organizers`, `/admin/organizers/{id}/verify` | Admin only |

Errors use one envelope: `{"error": {"code", "message", "request_id"}}`; the id is also returned as `X-Request-ID`.

## Repository layout

```
backend/   app/ (api, services, models, db, errors, seed) · alembic/ · tests/
frontend/  src/ (pages, components, lib)
docs/      SPEC, ARCHITECTURE, DECISIONS, LOCAL_VERIFICATION, runbook
Dockerfile · docker-compose.yml · .github/workflows/ci.yml
```

## Verification status

Verified locally: lint, strict typing, all 76 tests on Python 3.12 + PostgreSQL 16, frontend type-check/lint/build, multi-stage Docker build (`node:20-slim` + `python:3.12-slim`) with `/healthz` healthcheck, and a full browser walkthrough across Attendee, Organizer, and Admin flows. **Pending:** GitHub Actions CI run and live cloud deployment.
