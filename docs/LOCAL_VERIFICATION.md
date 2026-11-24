# Local verification

A manual walkthrough you can run yourself to confirm the behaviour. Assumes the setup in the README is running (API and UI on http://localhost:8000 after `npm run build`, or the UI on :5173 in dev).

## 1. Automated gates

```bash
docker compose up -d db-test
cd backend
ruff check . && ruff format --check . && mypy app tests
TEST_DATABASE_URL=postgresql://eventseat:eventseat@localhost:5433/eventseat_test pytest -q
cd ../frontend && npx tsc --noEmit && npm run lint && npm run build
```

Expected: all checks pass and `76 passed`.

## 2. The race, in the UI

1. Click **Attendee** under "Demo login".
2. Open any event, expand **Concurrency demo: 20 users, 1 seat**, choose a seat, press **Run race**.
3. Expected: `HTTP 201: 1`, `HTTP 409: 19`, `Double bookings: 0`, `Active rows for the seat: 1`. The winning hold is released afterwards, so the seat returns to Available.

## 3. The race, from the shell

```bash
curl -s -c jar -X POST localhost:8000/api/v1/auth/demo-login \
  -H 'Origin: http://localhost:8000' -H 'Content-Type: application/json' -d '{"persona":"ATTENDEE"}'
```

Take an event id from `GET /api/v1/events` and a seat id from `GET /api/v1/events/{id}/seats`, then `POST /api/v1/events/{id}/simulate-concurrency` with `{"seat_id": "..."}` (same `Origin` header and cookie jar).

## 4. Idempotency

1. Select two seats and press **Hold for 5 minutes**; the countdown starts at about 5:00.
2. Press **Pay & confirm**; a ticket code appears.
3. Press **Send the same request again**: the same ticket code returns with an **Idempotent replay** badge (the server sent `Idempotent-Replayed: true`).

Raw check: send `POST /bookings/hold` twice with the same `Idempotency-Key` and body: same `booking_id`, header on the second. Change the seat with the same key: `409 IDEMPOTENCY_MISMATCH`.

## 5. Expiry on the SQL clock

```sql
-- after creating a hold
UPDATE bookings SET held_until = statement_timestamp() - interval '1 second' WHERE status = 'HELD';
```

Within 5 s the seat map shows the seat Available again. Confirming that booking returns `410` and the row becomes EXPIRED.

## 6. Database guards (bypassing the app)

```bash
psql "$DATABASE_URL" -c '\d booking_seats'
```

Expected indexes include `uq_booking_seats_active_seat UNIQUE (event_id, seat_id) WHERE is_active = true` and the two composite foreign keys. Inserting a second active row for the same seat fails with a unique violation.

## 7. RBAC

- Register with `role: "SUPER_ADMIN"` returns `422`.
- Register a new **Organizer** account (via **Create account**): before verification, the Admin console shows "awaiting verification" and creating an event is `403`. As **Admin**, verify that organizer; creating an event then succeeds.
- Confirming or cancelling another user's booking returns `404` for a missing booking and `403` for someone else's.
