"""T5 expiry, T6 cancel and re-book, T9 sweeper, T10 event cancellation."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable

import httpx
from sqlalchemy import text

from app.services.expiry import run_sweeper_once
from tests.conftest import API, Actor, EventFx, SessionFactory

Rows = Callable[..., Awaitable[list]]  # type: ignore[type-arg]
EXPIRE_HOLD = (
    "UPDATE bookings SET held_until = statement_timestamp() - interval '1 second'"
    " WHERE id = CAST(:b AS uuid)"
)


async def seat_statuses(client: httpx.AsyncClient, event: EventFx) -> dict[str, str]:
    response = await client.get(f"{API}/events/{event.id}/seats")
    return {s["seat_label"]: s["status"] for s in response.json()["seats"]}


async def booking_row(sql: Rows, booking_id: str) -> tuple[str, int]:
    """(status, number of active booking_seats) for one booking."""
    rows = await sql(
        "SELECT b.status,"
        " (SELECT count(*) FROM booking_seats WHERE booking_id = b.id AND is_active)"
        " FROM bookings b WHERE b.id = CAST(:b AS uuid)",
        b=booking_id,
    )
    return rows[0][0], rows[0][1]


# --- T5 expiry ------------------------------------------------------------------------------


async def test_expired_hold_reads_as_available_at_once_and_confirm_is_410(
    client: httpx.AsyncClient,
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    confirm: Callable[..., Awaitable[httpx.Response]],
    sql: Rows,
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    booking_id = (await hold(alice, event, ["A1"])).json()["booking_id"]
    assert (await seat_statuses(client, event))["A1"] == "HELD"

    await sql(EXPIRE_HOLD, b=booking_id)

    assert (await seat_statuses(client, event))["A1"] == "AVAILABLE"
    assert (await booking_row(sql, booking_id)) == (
        "HELD",
        1,
    )  # nothing swept, reads are ghost-free

    late = await confirm(alice, booking_id)
    assert late.status_code == 410
    assert late.json()["error"]["code"] == "BOOKING_HOLD_EXPIRED"
    assert (await booking_row(sql, booking_id)) == ("EXPIRED", 0)  # committed with the 410


async def test_another_user_can_take_an_expired_seat_without_the_sweeper(
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    sql: Rows,
) -> None:
    alice, bob = await create_user(), await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    alice_booking = (await hold(alice, event, ["A1"])).json()["booking_id"]
    await sql(EXPIRE_HOLD, b=alice_booking)

    taken = await hold(bob, event, ["A1"])

    assert taken.status_code == 201
    assert (await booking_row(sql, alice_booking)) == ("EXPIRED", 0)
    active = await sql(
        "SELECT count(*) FROM booking_seats WHERE seat_id = CAST(:s AS uuid) AND is_active",
        s=event.seats["A1"],
    )
    assert active[0][0] == 1


async def test_a_user_whose_own_hold_lapsed_can_hold_different_seats(
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    sql: Rows,
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    first = (await hold(alice, event, ["A1"])).json()["booking_id"]
    await sql(EXPIRE_HOLD, b=first)

    second = await hold(alice, event, ["A2"])

    assert (
        second.status_code == 201
    )  # lazy expiry cleared the old HELD row, so the unique index holds
    assert (await booking_row(sql, first)) == ("EXPIRED", 0)


async def test_confirm_outcomes_by_booking_state(
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    confirm: Callable[..., Awaitable[httpx.Response]],
    client: httpx.AsyncClient,
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    booking_id = (await hold(alice, event, ["A1"])).json()["booking_id"]

    declined = await confirm(alice, booking_id, "FAILURE")
    paid = await confirm(alice, booking_id)
    again = await confirm(alice, booking_id)
    cancelled = await client.post(f"{API}/bookings/{booking_id}/cancel", headers=alice.headers)
    after_cancel = await confirm(alice, booking_id)

    assert declined.status_code == 402
    assert declined.json()["error"]["code"] == "PAYMENT_SIMULATION_FAILED"
    assert paid.status_code == 200
    assert paid.json()["status"] == "CONFIRMED"
    assert paid.json()["ticket_code"].startswith("EVT-")
    assert (again.status_code, again.json()["error"]["code"]) == (409, "BOOKING_ALREADY_CONFIRMED")
    assert cancelled.status_code == 200
    assert (after_cancel.status_code, after_cancel.json()["error"]["code"]) == (
        409,
        "BOOKING_CANCELLED",
    )


# --- T6 cancel and re-book ------------------------------------------------------------------


async def test_cancelled_seat_is_rebooked_and_both_rows_are_kept(
    client: httpx.AsyncClient,
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    confirm: Callable[..., Awaitable[httpx.Response]],
    sql: Rows,
) -> None:
    alice, bob = await create_user(), await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    a_id = (await hold(alice, event, ["A1"])).json()["booking_id"]
    await confirm(alice, a_id)
    assert (await seat_statuses(client, event))["A1"] == "BOOKED"

    cancel = await client.post(f"{API}/bookings/{a_id}/cancel", headers=alice.headers)
    assert cancel.status_code == 200
    assert (await seat_statuses(client, event))["A1"] == "AVAILABLE"

    b_id = (await hold(bob, event, ["A1"])).json()["booking_id"]
    assert (await confirm(bob, b_id)).status_code == 200

    assert (await booking_row(sql, a_id)) == ("CANCELLED", 0)
    assert (await booking_row(sql, b_id)) == ("CONFIRMED", 1)
    assert (await sql("SELECT count(*) FROM booking_seats"))[0][0] == 2


async def test_cancel_is_idempotent_and_admin_may_cancel_any_booking(
    client: httpx.AsyncClient,
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
) -> None:
    alice = await create_user()
    admin = await create_user("SUPER_ADMIN")
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    booking_id = (await hold(alice, event, ["A1"])).json()["booking_id"]
    url = f"{API}/bookings/{booking_id}/cancel"

    by_admin = await client.post(url, headers=admin.headers)
    again = await client.post(url, headers=alice.headers)
    missing = await client.post(f"{API}/bookings/{uuid.uuid4()}/cancel", headers=alice.headers)

    assert by_admin.status_code == again.status_code == 200
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "BOOKING_NOT_FOUND"


async def test_cancelling_an_expired_hold_is_409_and_commits_the_expiry(
    client: httpx.AsyncClient,
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    sql: Rows,
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    booking_id = (await hold(alice, event, ["A1"])).json()["booking_id"]
    await sql(EXPIRE_HOLD, b=booking_id)

    response = await client.post(f"{API}/bookings/{booking_id}/cancel", headers=alice.headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "BOOKING_ALREADY_EXPIRED"
    assert (await booking_row(sql, booking_id)) == ("EXPIRED", 0)


async def test_cancel_after_the_event_started_is_409(
    client: httpx.AsyncClient,
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    confirm: Callable[..., Awaitable[httpx.Response]],
    sql: Rows,
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    booking_id = (await hold(alice, event, ["A1"])).json()["booking_id"]
    await confirm(alice, booking_id)
    await sql(
        "UPDATE events SET booking_closes_at = statement_timestamp() - interval '2 hours',"
        " starts_at = statement_timestamp() - interval '1 hour' WHERE id = :id",
        id=event.id,
    )

    response = await client.post(f"{API}/bookings/{booking_id}/cancel", headers=alice.headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVENT_ALREADY_STARTED"
    assert (await booking_row(sql, booking_id)) == ("CONFIRMED", 1)


async def test_my_bookings_sorts_tickets_live_holds_and_history(
    client: httpx.AsyncClient,
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    confirm: Callable[..., Awaitable[httpx.Response]],
    sql: Rows,
) -> None:
    alice = await create_user()
    organizer = await create_user("ORGANIZER", verified=True)
    ticket_event = await create_event(organizer.id)
    hold_event = await create_event(organizer.id)
    lapsed_event = await create_event(organizer.id)
    ticket_id = (await hold(alice, ticket_event, ["A1", "A2"])).json()["booking_id"]
    await confirm(alice, ticket_id)
    live = (await hold(alice, hold_event, ["B1"])).json()
    lapsed_id = (await hold(alice, lapsed_event, ["A1"])).json()["booking_id"]
    await sql(EXPIRE_HOLD, b=lapsed_id)

    body = (await client.get(f"{API}/bookings/me", headers=alice.headers)).json()

    assert [t["seat_labels"] for t in body["tickets"]] == [["A1", "A2"]]
    assert [h["id"] for h in body["holds"]] == [live["booking_id"]]
    assert 0 < body["holds"][0]["remaining_seconds"] <= 300
    assert [(h["id"], h["status"]) for h in body["history"]] == [(lapsed_id, "EXPIRED")]
    assert (await client.get(f"{API}/bookings/me")).status_code == 401


# --- T9 sweeper -----------------------------------------------------------------------------


async def test_sweeper_expires_stale_holds_and_prunes_old_idempotency_records(
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    session_factory: SessionFactory,
    sql: Rows,
) -> None:
    alice, bob = await create_user(), await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    stale = (await hold(alice, event, ["A1"], key="stale")).json()["booking_id"]
    fresh = (await hold(bob, event, ["B1"], key="fresh")).json()["booking_id"]
    await sql(EXPIRE_HOLD, b=stale)
    await sql(
        "UPDATE idempotency_records"
        " SET expires_at = statement_timestamp() - interval '1 hour'"
        " WHERE idempotency_key = 'stale'"
    )

    result = await run_sweeper_once(session_factory)

    assert (result.expired_holds, result.pruned_idempotency_records) == (1, 1)
    assert (await booking_row(sql, stale)) == ("EXPIRED", 0)
    assert (await booking_row(sql, fresh)) == ("HELD", 1)
    assert [r[0] for r in await sql("SELECT idempotency_key FROM idempotency_records")] == ["fresh"]


async def test_sweeper_skips_a_booking_locked_by_another_transaction(
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    session_factory: SessionFactory,
    sql: Rows,
) -> None:
    alice = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    booking_id = (await hold(alice, event, ["A1"])).json()["booking_id"]
    await sql(EXPIRE_HOLD, b=booking_id)

    blocker = session_factory()
    async with blocker, blocker.begin():
        await blocker.execute(
            text("SELECT id FROM bookings WHERE id = CAST(:b AS uuid) FOR UPDATE"),
            {"b": booking_id},
        )
        skipped = await asyncio.wait_for(run_sweeper_once(session_factory), timeout=5)
    swept = await run_sweeper_once(session_factory)

    assert skipped.expired_holds == 0
    assert swept.expired_holds == 1
    assert (await booking_row(sql, booking_id)) == ("EXPIRED", 0)


# --- T10 event cancellation -----------------------------------------------------------------


async def test_cancelling_an_event_releases_every_seat_and_is_idempotent(
    client: httpx.AsyncClient,
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    confirm: Callable[..., Awaitable[httpx.Response]],
    sql: Rows,
) -> None:
    organizer = await create_user("ORGANIZER", verified=True)
    rival = await create_user("ORGANIZER", verified=True)
    alice, bob = await create_user(), await create_user()
    event = await create_event(organizer.id)
    confirmed = (await hold(alice, event, ["A1", "A2"])).json()["booking_id"]
    await confirm(alice, confirmed)
    held = (await hold(bob, event, ["B1"])).json()["booking_id"]
    url = f"{API}/events/{event.id}/cancel"

    forbidden = [
        await client.post(url, headers=rival.headers),
        await client.post(url, headers=alice.headers),
    ]
    first = await client.post(url, headers=organizer.headers)
    second = await client.post(url, headers=organizer.headers)
    missing = await client.post(f"{API}/events/{uuid.uuid4()}/cancel", headers=organizer.headers)

    assert [r.status_code for r in forbidden] == [403, 403]
    assert (first.status_code, second.status_code, missing.status_code) == (200, 200, 404)
    assert (await booking_row(sql, confirmed)) == ("CANCELLED", 0)
    assert (await booking_row(sql, held)) == ("CANCELLED", 0)
    assert (await sql("SELECT status FROM events WHERE id = :id", id=event.id))[0][0] == "CANCELLED"
    late = await hold(alice, event, ["C1"] if "C1" in event.seats else ["A3"])
    assert (late.status_code, late.json()["error"]["code"]) == (409, "EVENT_NOT_PUBLISHED")


async def test_concurrent_hold_and_event_cancel_never_deadlock_or_leave_active_seats(
    client: httpx.AsyncClient,
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    sql: Rows,
) -> None:
    organizer = await create_user("ORGANIZER", verified=True)
    attendee = await create_user()
    for _ in range(8):
        event = await create_event(organizer.id)
        held, cancelled = await asyncio.gather(
            hold(attendee, event, ["A1", "A2"]),
            client.post(f"{API}/events/{event.id}/cancel", headers=organizer.headers),
        )
        assert held.status_code in (201, 409)
        assert cancelled.status_code == 200
        active = await sql(
            "SELECT count(*) FROM booking_seats WHERE event_id = :e AND is_active", e=event.id
        )
        assert active[0][0] == 0
