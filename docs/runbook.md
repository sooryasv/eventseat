# Runbook

Operational notes for a single-instance deployment of the container image.

## Start-up

The container runs `alembic upgrade head` and then uvicorn. If migration fails the container exits, so a bad schema never serves traffic. Health: `GET /healthz` returns 200 (also used by the image `HEALTHCHECK`).

## Required configuration

| Variable | Notes |
| --- | --- |
| `DATABASE_URL` | PostgreSQL URL. `postgres://` / `postgresql://` schemes are accepted; `sslmode` and `channel_binding` are stripped and mapped to asyncpg SSL. |
| `JWT_SECRET` | Must differ from the dev default when `COOKIE_SECURE=true`, otherwise the app refuses to start. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `COOKIE_SECURE` | `true` behind HTTPS. |
| `ALLOWED_ORIGINS` | Comma-separated exact origins (scheme + host + port) of the public URL. A mismatch causes `403` on every POST. |
| `USE_NULL_POOL` | `true` for serverless or transaction-pooled Postgres. |
| `ENABLE_DEMO_TOOLS` | `false` in any real deployment: disables demo login and the simulator (`404`) and emits `X-Demo-Tools: false` so the UI hides both controls. |
| `RUN_SEED_ON_STARTUP` | Seeds demo events and users idempotently; set `false` for real data. |
| `HOLD_TTL_SECONDS`, `SWEEPER_INTERVAL_SECONDS` | Defaults 300 and 60. On scale-to-zero serverless Postgres (e.g. Neon free tier), raise `SWEEPER_INTERVAL_SECONDS` (e.g. `3600`) so the sweeper does not keep compute awake 24/7; seat expiry remains immediate via lazy SQL-clock evaluation. |

## Symptoms and checks

**Every write returns 403 `CSRF_ORIGIN_REJECTED`.** `ALLOWED_ORIGINS` does not contain the exact origin the browser uses. Fix the variable and restart.

**Container exits immediately.** Read the logs: either `alembic upgrade head` failed (database unreachable or credentials wrong) or `JWT_SECRET` is still the dev value with `COOKIE_SECURE=true`.

**Seats look stuck as HELD.** Expired holds are treated as free on read and on the next hold or confirm, so this is cosmetic at worst. Check the sweeper is alive: logs show a `sweeper_pass` event every `SWEEPER_INTERVAL_SECONDS`, or `sweeper_failed` with a traceback on a database error (the loop keeps going and retries on the next interval). If neither appears, restart the app.

**Suspected deadlock.** Look for SQLSTATE `40P01` in the Postgres logs. The lock hierarchy should make this impossible; if it appears, treat it as a bug and inspect the most recent change to the lock order (each lock site is tagged `# L1`..`# L5`).

**Check for double bookings directly.**

```sql
SELECT event_id, seat_id, count(*) FROM booking_seats WHERE is_active GROUP BY 1, 2 HAVING count(*) > 1;
```

Must return no rows; the unique index makes it impossible by construction.

**Find a request.** Every response carries `X-Request-ID`; error bodies repeat it as `request_id`. Search logs for that value.

## Bootstrapping the first SUPER_ADMIN (real mode)

When `ENABLE_DEMO_TOOLS=false` and `RUN_SEED_ON_STARTUP=false`, register a standard account through the UI (`POST /api/v1/auth/register` forbids `SUPER_ADMIN`), then promote it once in SQL:

```sql
UPDATE users SET role = 'SUPER_ADMIN' WHERE email = 'admin@example.com';
```

Sign out and sign back in so a fresh JWT cookie is issued with the `SUPER_ADMIN` role.

## Limits to know about

- The simulator's single-flight lock is per process; with several instances, disable demo tools.
- Sessions are one JWT per login (60 min default); there are no refresh tokens.
- No rate limiting or login lockout is built in; put a gateway in front for public deployments.
- Rolling back the schema: `alembic downgrade base` drops all tables, so use it only on disposable databases.
