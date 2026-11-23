"""T1 race, T2 deadlock freedom, T3 idempotency, T4 hoarding, T8 DB guards, T11 simulator."""

import asyncio
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError

from app.seed import run_seed
from app.services import concurrency_demo
from app.services.idempotency import IdempotencyScope, is_persistable, store_result
from tests.conftest import API, Actor, EventFx, SessionFactory

Rows = Callable[..., Awaitable[list]]  # type: ignore[type-arg]
Hold = Callable[..., Awaitable[httpx.Response]]
Confirm = Callable[..., Awaitable[httpx.Response]]
MakeUser = Callable[..., Awaitable[Actor]]
MakeEvent = Callable[..., Awaitable[EventFx]]


async def active_rows(sql: Rows, seat_id: uuid.UUID) -> int:
    rows = await sql(
        "SELECT count(*) FROM booking_seats WHERE seat_id = :s AND is_active", s=seat_id
    )
    return int(rows[0][0])


# --- T1 race --------------------------------------------------------------------------------


async def test_twenty_concurrent_holds_yield_one_winner(
    create_user: MakeUser, create_event: MakeEvent, hold: Hold, sql: Rows
) -> None:
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    users = [await create_user() for _ in range(20)]

    responses = await asyncio.gather(*(hold(user, event, ["A1"]) for user in users))

    assert Counter(r.status_code for r in responses) == {201: 1, 409: 19}
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {
        "SEAT_UNAVAILABLE"
    }
    assert await active_rows(sql, event.seats["A1"]) == 1


# --- T2 deadlock freedom --------------------------------------------------------------------


async def test_opposite_order_multi_seat_holds_never_deadlock(
    create_user: MakeUser, create_event: MakeEvent, hold: Hold
) -> None:
    organizer = await create_user("ORGANIZER", verified=True)
    alice, bob = await create_user(), await create_user()
    for trial in range(10):
        event = await create_event(organizer.id)
        first, second = await asyncio.gather(
            hold(alice, event, ["A1", "A2"]), hold(bob, event, ["A2", "A1"])
        )
        statuses = sorted([first.status_code, second.status_code])
        assert statuses == [201, 409], f"trial {trial}: {statuses}"  # a 40P01 would surface as 500
        loser = first if first.status_code == 409 else second
        assert loser.json()["error"]["code"] == "SEAT_UNAVAILABLE"


async def test_overlapping_holds_from_many_users_all_resolve_cleanly(
    create_user: MakeUser, create_event: MakeEvent, hold: Hold, sql: Rows
) -> None:
    event = await create_event((await create_user("ORGANIZER", verified=True)).id, rows=2, cols=4)
    users = [await create_user() for _ in range(12)]
    shapes = [["A1", "A2"], ["A2", "A3"], ["A3", "A1"], ["B1", "A1"], ["A3", "B1"], ["A2", "B1"]]

    responses = await asyncio.gather(
        *(
            hold(user, event, shapes[i % len(shapes)][:: 1 if i % 2 else -1])
            for i, user in enumerate(users)
        )
    )

    assert all(r.status_code in (201, 409) for r in responses)
    most_per_seat = await sql(
        "SELECT max(n) FROM (SELECT count(*) n FROM booking_seats WHERE is_active"
        " GROUP BY seat_id) t"
    )
    assert most_per_seat[0][0] == 1


# --- T3 idempotency -------------------------------------------------------------------------


async def test_concurrent_identical_requests_create_one_booking_and_replay_the_rest(
    create_user: MakeUser, create_event: MakeEvent, hold: Hold, sql: Rows
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)

    responses = await asyncio.gather(*(hold(alice, event, ["A1"], key="same") for _ in range(5)))

    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["booking_id"] for r in responses}) == 1
    assert (
        sorted(r.headers.get("idempotent-replayed") or "no" for r in responses)
        == ["no"] + ["true"] * 4
    )
    assert (await sql("SELECT count(*) FROM bookings"))[0][0] == 1
    assert (await sql("SELECT count(*) FROM idempotency_records"))[0][0] == 1


async def test_same_key_with_a_different_payload_is_a_409_mismatch_and_not_stored(
    create_user: MakeUser, create_event: MakeEvent, hold: Hold, sql: Rows
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    await hold(alice, event, ["A1"], key="k")

    mismatch = await hold(alice, event, ["A2"], key="k")

    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "IDEMPOTENCY_MISMATCH"
    assert "idempotent-replayed" not in mismatch.headers
    assert (await sql("SELECT count(*) FROM idempotency_records"))[0][0] == 1


async def test_seat_order_does_not_change_the_request_hash(
    create_user: MakeUser, create_event: MakeEvent, hold: Hold
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    first = await hold(alice, event, ["A1", "A2"], key="k")
    reversed_order = await hold(alice, event, ["A2", "A1"], key="k")

    assert first.status_code == reversed_order.status_code == 201
    assert reversed_order.headers["idempotent-replayed"] == "true"
    assert reversed_order.json() == first.json()


async def test_a_seat_unavailable_409_is_persisted_and_replayed(
    create_user: MakeUser, create_event: MakeEvent, hold: Hold, sql: Rows
) -> None:
    alice, bob = await create_user(), await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    await hold(alice, event, ["A1"])

    first = await hold(bob, event, ["A1"], key="bob-key")
    replay = await hold(bob, event, ["A1"], key="bob-key")

    assert (first.status_code, replay.status_code) == (409, 409)
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.json() == first.json()
    stored = await sql(
        "SELECT status_code FROM idempotency_records WHERE idempotency_key = 'bob-key'"
    )
    assert [r[0] for r in stored] == [409]


async def test_a_402_is_persisted_replayed_and_a_new_key_may_retry(
    create_user: MakeUser, create_event: MakeEvent, hold: Hold, confirm: Confirm, sql: Rows
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    booking_id = (await hold(alice, event, ["A1"])).json()["booking_id"]

    declined = await confirm(alice, booking_id, "FAILURE", key="pay-1")
    replay = await confirm(alice, booking_id, "FAILURE", key="pay-1")
    retry = await confirm(alice, booking_id, "SUCCESS", key="pay-2")

    assert (declined.status_code, replay.status_code, retry.status_code) == (402, 402, 200)
    assert replay.headers["idempotent-replayed"] == "true"
    assert (await sql("SELECT status FROM bookings"))[0][0] == "CONFIRMED"


async def test_confirm_replay_returns_the_same_ticket_without_a_second_confirmation(
    create_user: MakeUser, create_event: MakeEvent, hold: Hold, confirm: Confirm
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    booking_id = (await hold(alice, event, ["A1"])).json()["booking_id"]

    responses = await asyncio.gather(*(confirm(alice, booking_id, key="pay") for _ in range(4)))

    assert {r.status_code for r in responses} == {200}
    assert len({r.json()["ticket_code"] for r in responses}) == 1


async def test_an_expired_record_is_pruned_and_the_request_runs_again(
    create_user: MakeUser, create_event: MakeEvent, hold: Hold, sql: Rows
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    first = await hold(alice, event, ["A1"], key="old")
    await sql(
        "UPDATE idempotency_records SET expires_at = statement_timestamp() - interval '1 second'"
    )

    rerun = await hold(alice, event, ["A1"], key="old")

    assert first.status_code == 201
    assert rerun.status_code == 409  # genuinely re-executed: Alice already holds A1
    assert "idempotent-replayed" not in rerun.headers
    assert [r[0] for r in await sql("SELECT status_code FROM idempotency_records")] == [409]


async def test_server_errors_can_never_be_persisted(session_factory: SessionFactory) -> None:
    assert [is_persistable(code) for code in (200, 201, 402, 409, 410)] == [True] * 5
    assert [is_persistable(code) for code in (403, 404, 422, 500, 503)] == [False] * 5
    scope = IdempotencyScope(uuid.uuid4(), "hold", "k", "0" * 64)
    async with session_factory() as session:
        with pytest.raises(ValueError, match="never be persisted"):
            await store_result(session, scope, 500, {})


async def test_hold_requires_a_bounded_idempotency_key(
    client: httpx.AsyncClient, create_user: MakeUser, create_event: MakeEvent
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    body = {"event_id": str(event.id), "seat_ids": [str(event.seats["A1"])]}
    too_long = await client.post(
        f"{API}/bookings/hold", json=body, headers={**alice.headers, "Idempotency-Key": "k" * 129}
    )
    empty = await client.post(
        f"{API}/bookings/hold", json=body, headers={**alice.headers, "Idempotency-Key": ""}
    )
    assert too_long.status_code == empty.status_code == 422


# --- T4 hoarding cap ------------------------------------------------------------------------


async def test_two_simultaneous_holds_by_one_user_yield_one_winner(
    create_user: MakeUser, create_event: MakeEvent, hold: Hold, sql: Rows
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)

    first, second = await asyncio.gather(hold(alice, event, ["A1"]), hold(alice, event, ["B1"]))

    assert sorted([first.status_code, second.status_code]) == [201, 409]
    loser = first if first.status_code == 409 else second
    assert loser.json()["error"]["code"] == "USER_LIMIT_EXCEEDED"
    assert (await sql("SELECT count(*) FROM bookings"))[0][0] == 1


async def test_three_confirmed_seats_block_a_hold_of_two_more(
    create_user: MakeUser, create_event: MakeEvent, hold: Hold, confirm: Confirm
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    booking_id = (await hold(alice, event, ["A1", "A2", "A3"])).json()["booking_id"]
    await confirm(alice, booking_id)

    too_many = await hold(alice, event, ["A4", "B1"])
    one_more = await hold(alice, event, ["A4"])

    assert too_many.status_code == 409
    assert too_many.json()["error"]["code"] == "USER_LIMIT_EXCEEDED"
    assert one_more.status_code == 201  # 3 confirmed + 1 held = the cap of 4
    await confirm(alice, one_more.json()["booking_id"])
    fifth = await hold(alice, event, ["B2"])
    assert (fifth.status_code, fifth.json()["error"]["code"]) == (409, "USER_LIMIT_EXCEEDED")


async def test_more_than_four_seats_in_one_request_is_422(
    create_user: MakeUser, create_event: MakeEvent, hold: Hold
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    five = await hold(alice, event, ["A1", "A2", "A3", "A4", "B1"])
    four = await hold(alice, event, ["A1", "A2", "A3", "A4"])
    assert (five.status_code, four.status_code) == (422, 201)


# --- T8 DB-level guards ---------------------------------------------------------------------


async def violation(sql: Rows, statement: str, constraint: str, **params: object) -> None:
    with pytest.raises(IntegrityError) as caught:
        await sql(statement, **params)
    assert constraint in str(caught.value.orig)


async def insert_booking(sql: Rows, user: Actor, event: EventFx, status: str = "HELD") -> uuid.UUID:
    booking_id = uuid.uuid4()
    await sql(
        "INSERT INTO bookings (id, user_id, event_id, status, held_until)"
        " VALUES (:id, :u, :e, :s, statement_timestamp() + interval '5 minutes')",
        id=booking_id, u=user.id, e=event.id, s=status,
    )  # fmt: skip
    return booking_id


async def test_postgres_rejects_two_active_rows_for_one_seat(
    create_user: MakeUser, create_event: MakeEvent, sql: Rows
) -> None:
    alice, bob = await create_user(), await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    seat = event.seats["A1"]
    insert_seat = (
        "INSERT INTO booking_seats (booking_id, event_id, seat_id, is_active)"
        " VALUES (:b, :e, :s, :active)"
    )
    first = await insert_booking(sql, alice, event)
    second = await insert_booking(sql, bob, event)
    await sql(insert_seat, b=first, e=event.id, s=seat, active=True)

    await violation(sql, insert_seat, "uq_booking_seats_active_seat",
                    b=second, e=event.id, s=seat, active=True)  # fmt: skip
    await sql(insert_seat, b=second, e=event.id, s=seat, active=False)  # inactive rows may coexist


async def test_postgres_rejects_a_second_held_booking_per_user_and_event(
    create_user: MakeUser, create_event: MakeEvent, sql: Rows
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    await insert_booking(sql, alice, event)
    with pytest.raises(IntegrityError) as caught:
        await insert_booking(sql, alice, event)
    assert "uq_bookings_one_active_hold_per_user_event" in str(caught.value.orig)


async def test_composite_foreign_keys_forbid_crossing_events(
    create_user: MakeUser, create_event: MakeEvent, sql: Rows
) -> None:
    alice = await create_user()
    organizer = await create_user("ORGANIZER", verified=True)
    event_a, event_b = await create_event(organizer.id), await create_event(organizer.id)
    booking_a = await insert_booking(sql, alice, event_a)
    insert_seat = "INSERT INTO booking_seats (booking_id, event_id, seat_id) VALUES (:b, :e, :s)"

    await violation(sql, insert_seat, "fk_booking_seats_seat_event",
                    b=booking_a, e=event_a.id, s=event_b.seats["A1"])  # fmt: skip
    await violation(sql, insert_seat, "fk_booking_seats_booking_event",
                    b=booking_a, e=event_b.id, s=event_b.seats["A1"])  # fmt: skip


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        (
            "INSERT INTO bookings (user_id, event_id, status) VALUES (:u, :e, 'HELD')",
            "ck_bookings_held_has_expiry",
        ),
        (
            "INSERT INTO bookings (user_id, event_id, status) VALUES (:u, :e, 'CONFIRMED')",
            "ck_bookings_confirmed_fields",
        ),
        (
            "INSERT INTO bookings (user_id, event_id, status) VALUES (:u, :e, 'CANCELLED')",
            "ck_bookings_cancelled_fields",
        ),
        (
            "INSERT INTO bookings (user_id, event_id, status) VALUES (:u, :e, 'BOGUS')",
            "bookings_status_check",
        ),
        (
            "INSERT INTO events"
            " (organizer_id, title, category, venue, starts_at, booking_closes_at)"
            " VALUES (:u, 't', 'MEETUP', 'v', now(), now() + interval '1 day')",
            "ck_events_cutoff_before_start",
        ),
        (
            "INSERT INTO users (email, full_name, password_hash, role)"
            " VALUES ('Mixed@Case.dev', 'n', 'h', 'ATTENDEE')",
            "ck_users_email_lowercase",
        ),
        (
            "INSERT INTO users (email, full_name, password_hash, role)"
            " VALUES ('a@b.dev', 'n', 'h', 'ROOT')",
            "users_role_check",
        ),
        (
            "INSERT INTO idempotency_records (user_id, endpoint, idempotency_key, request_hash,"
            " status_code, response_body) VALUES (:u, 'hold', 'k', repeat('0', 64), 500, '{}')",
            "idempotency_records_status_code_check",
        ),
    ],
)
async def test_postgres_check_constraints_reject_invalid_rows(
    create_user: MakeUser,
    create_event: MakeEvent,
    sql: Rows,
    statement: str,
    constraint: str,
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    await violation(
        sql, statement, constraint, u=alice.id, **({"e": event.id} if ":e" in statement else {})
    )


# --- T11 simulator --------------------------------------------------------------------------


async def simulate(
    client: httpx.AsyncClient, actor: Actor, event: EventFx, label: str = "A1"
) -> httpx.Response:
    return await client.post(
        f"{API}/events/{event.id}/simulate-concurrency",
        json={"seat_id": str(event.seats[label])},
        headers=actor.headers,
    )


async def test_simulator_reports_one_winner_and_releases_the_winning_hold(
    client: httpx.AsyncClient,
    session_factory: SessionFactory,
    create_user: MakeUser,
    create_event: MakeEvent,
    sql: Rows,
) -> None:
    await run_seed(session_factory)
    viewer = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)

    for _ in range(2):  # the second run proves the demo accounts were not left poisoned
        response = await simulate(client, viewer, event)
        body = response.json()
        assert response.status_code == 200
        assert body["status_counts"] == {"201": 1, "409": 19}
        assert (body["double_bookings"], body["active_seat_rows"]) == (0, 1)
        assert (body["released_holds"], body["duration_ms"] >= 0) == (1, True)
        assert await active_rows(sql, event.seats["A1"]) == 0


async def test_a_second_simulation_while_one_is_running_is_429(
    client: httpx.AsyncClient, create_user: MakeUser, create_event: MakeEvent
) -> None:
    viewer = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)

    async with concurrency_demo.DEMO_LOCK:
        response = await simulate(client, viewer, event)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "DEMO_BUSY"


async def test_simulator_is_404_when_demo_tools_are_disabled(
    make_app: Callable[..., Awaitable[FastAPI]],
    make_client: Callable[..., Awaitable[httpx.AsyncClient]],
) -> None:
    app = await make_app(enable_demo_tools=False)
    client = await make_client(app)
    response = await client.post(
        f"{API}/events/{uuid.uuid4()}/simulate-concurrency", json={"seat_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404


async def test_simulator_needs_auth_and_a_real_seat(
    client: httpx.AsyncClient,
    session_factory: SessionFactory,
    create_user: MakeUser,
    create_event: MakeEvent,
) -> None:
    await run_seed(session_factory)
    viewer = await create_user()
    organizer = await create_user("ORGANIZER", verified=True)
    event, other = await create_event(organizer.id), await create_event(organizer.id)
    url = f"{API}/events/{event.id}/simulate-concurrency"

    anonymous = await client.post(url, json={"seat_id": str(event.seats["A1"])})
    foreign_seat = await simulate(client, viewer, EventFx(event.id, other.seats))
    no_event = await client.post(
        f"{API}/events/{uuid.uuid4()}/simulate-concurrency",
        json={"seat_id": str(event.seats["A1"])},
        headers=viewer.headers,
    )

    assert anonymous.status_code == 401
    assert (foreign_seat.status_code, foreign_seat.json()["error"]["code"]) == (
        404,
        "SEAT_NOT_FOUND",
    )
    assert (no_event.status_code, no_event.json()["error"]["code"]) == (404, "EVENT_NOT_FOUND")
