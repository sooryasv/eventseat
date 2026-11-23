# Architecture Decision Records

## ADR-001: PostgreSQL only, no Redis

**Status:** Accepted

**Context.** Seat contention needs mutual exclusion, durable state and expiry. Redis is the common reflex for locks and TTLs.

**Decision.** Use PostgreSQL for everything: advisory locks for logical actors, row locks for seats, a partial unique index as the final guard, and a timestamp column plus a sweeper for expiry.

**Consequences.**
- One system to run, back up and reason about; a hold and its seat claim commit atomically, so they cannot disagree.
- Correctness does not depend on a cache being warm or a TTL firing on time.
- Tests run against the real engine, which is the thing that provides the guarantees.
- Cost: lock contention lands on the database. See ADR-002.

## ADR-002: Scaling toward 10k users, and what Redis would and would not fix

**Status:** Accepted (analysis; not load-tested)

**Context.** A popular on-sale means many users contending for a small set of seats.

**Decision.** Stay on PostgreSQL for v1. Locks are held for one short transaction, seats are locked in id order, and the sweeper never waits (`SKIP LOCKED`).

**Consequences.**
- Throughput is bounded by the connection pool and per-event row-lock queueing. Likely first steps: raise pool size behind PgBouncer, cache the read-only catalog, and serve seat-map reads from a replica.
- Redis would help with read caching, rate limiting and a cross-instance simulator lock.
- Redis would not replace the seat guarantee: a Redis lock still needs a database constraint underneath, so it adds a failure mode without removing the database write.
- No throughput numbers are claimed for v1; none were measured.

## ADR-003: Payment gateway extension (three-state flow)

**Status:** Proposed (not built; checkout is simulated)

**Context.** Real gateways are asynchronous: a payment can be pending, succeed later, or fail after the hold expired.

**Decision.** Keep the simulated confirm for v1. A real integration would add a PENDING_PAYMENT state, create the gateway order inside the hold window, and finalise through a signed webhook that is itself idempotent on the gateway event id.

**Consequences.**
- The existing idempotency layer and the `held_until > sql_now()` re-check carry over unchanged.
- A webhook arriving after expiry needs a refund path; that is the main new design work.
- Deferred to post-v1.

## ADR-004: Same-origin cookie authentication

**Status:** Accepted

**Context.** Tokens in `localStorage` are readable by any injected script.

**Decision.** Issue a JWT in an HttpOnly, SameSite=Lax cookie and serve the SPA and API from one origin. Defend state-changing requests with an Origin/Referer allow-list middleware.

**Consequences.**
- The browser code never touches a token; XSS cannot exfiltrate it.
- CSRF is covered by SameSite plus the Origin guard, with no bypass for missing headers.
- No refresh tokens in v1: a session lasts `JWT_EXPIRE_MINUTES` (default 60) and then requires login.
- Cross-origin deployments (separate frontend host) are not supported without revisiting this ADR.

## ADR-005: SQL clock semantics

**Status:** Accepted

**Context.** `NOW()` returns the transaction's start time. A request that waits two seconds on a lock would then judge expiry against a clock two seconds stale.

**Decision.** Every comparison against `held_until`, `booking_closes_at`, `starts_at` or `expires_at` runs in SQL using `sql_now()`, defined as `func.statement_timestamp()`. Python time is never used for a decision.

**Consequences.**
- Expiry is judged at the moment each statement runs, after any lock wait.
- Application-server clock skew cannot change an outcome.
- Confirm re-checks `held_until > sql_now()` inside its `UPDATE`, so a hold cannot be confirmed after it expires even if the earlier read was fine.
- Tests move time by editing `held_until`, not by patching a Python clock.

## ADR-006: Lock hierarchy and defence in depth

**Status:** Accepted

**Context.** Holds, confirms, cancels, event cancellation and the sweeper touch overlapping rows; inconsistent lock order causes deadlocks.

**Decision.** Always acquire in ascending order: L1 idempotency advisory, L2 hoarding advisory, L3 event row, L4 seat rows by id, L5 booking rows by id. Advisory locks serialise logical actors before any row is touched; seat rows are the exclusion primitive because they exist before any booking does. The partial unique index and composite foreign keys are a second, independent guard.

**Consequences.**
- Opposite-order requests (`[A1,A2]` vs `[A2,A1]`) queue instead of deadlocking; test T2 runs this 10 times.
- Test T8 bypasses the service layer and shows the database rejects violations on its own.
- Each lock site carries an `# L1`..`# L5` comment so reviews can check ordering.
- A mutation test (reversing an order) was caught by T2, which showed that the test can detect a deadlock.

## ADR-007: Modular monolith over microservices

**Status:** Accepted

**Context.** Booking, expiry and cancellation must share one transaction boundary.

**Decision.** One deployable with clear internal modules (routers, services, queries) and a single database.

**Consequences.**
- Cross-module invariants stay inside a transaction instead of becoming a distributed saga.
- One image, one migration path, simple local setup.
- If a boundary is ever extracted, the services layer already has narrow interfaces to cut along.
- Cost: no independent scaling of read and write paths until that extraction.
