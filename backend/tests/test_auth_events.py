"""T0 foundation, T7 security and RBAC, T12 read models."""

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import text

from app.config import Settings
from app.db import advisory_lock_key, advisory_xact_lock, normalise_database_url
from app.seed import run_seed
from tests.conftest import API, Actor, EventFx, SessionFactory

Rows = Callable[..., Awaitable[list]]  # type: ignore[type-arg]


def event_payload(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "title": "Rust Night",
        "category": "MEETUP",
        "venue": "Hall 3",
        "starts_at": (now + timedelta(days=30)).isoformat(),
        "booking_closes_at": (now + timedelta(days=29)).isoformat(),
        "rows": 2,
        "cols": 4,
    }
    return {**payload, **overrides}


def register_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "email": "ada@example.com",
        "password": "correct-horse-battery",
        "full_name": "Ada Lovelace",
        "role": "ATTENDEE",
    }
    return {**payload, **overrides}


# --- T0 foundation --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_url", "expected_ssl"),
    [
        ("postgresql://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d", False),
        ("postgres://u:p@h/d?sslmode=require", "postgresql+asyncpg://u:p@h/d", True),
        ("postgresql+asyncpg://u:p@h/d?sslmode=disable", "postgresql+asyncpg://u:p@h/d", False),
        (
            "postgresql://u:p@h/d?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://u:p@h/d",
            True,
        ),
        (
            "postgresql://u:p@h/d?application_name=x&sslmode=verify-full",
            "postgresql+asyncpg://u:p@h/d?application_name=x",
            True,
        ),
    ],
)
def test_database_url_is_normalised_for_asyncpg(
    raw: str, expected_url: str, expected_ssl: bool
) -> None:
    assert normalise_database_url(raw) == (expected_url, expected_ssl)


def test_advisory_lock_keys_are_domain_separated_signed_64_bit() -> None:
    idem = advisory_lock_key("idem", "x")
    hoard = advisory_lock_key("hoard", "x")
    assert idem != hoard
    assert all(-(2**63) <= key < 2**63 for key in (idem, hoard))
    assert advisory_lock_key("idem", "x") == idem


async def test_healthz_reports_ok_and_echoes_a_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert re.fullmatch(r"req_[0-9a-f]{8}", response.headers["x-request-id"])


async def test_errors_use_the_envelope_and_carry_the_same_request_id(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(f"{API}/auth/me")
    error = response.json()["error"]
    assert response.status_code == 401
    assert set(error) == {"code", "message", "request_id"}
    assert error["code"] == "UNAUTHENTICATED"
    assert error["request_id"] == response.headers["x-request-id"]


async def test_unknown_api_path_is_a_404_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API}/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_dev_jwt_secret_is_rejected_when_cookie_secure() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(_env_file=None, cookie_secure=True)
    Settings(_env_file=None, cookie_secure=True, jwt_secret="a-real-secret-" * 4)


# --- T7 auth, CSRF, RBAC --------------------------------------------------------------------


async def test_register_rejects_super_admin_and_unknown_fields(client: httpx.AsyncClient) -> None:
    admin = await client.post(f"{API}/auth/register", json=register_payload(role="SUPER_ADMIN"))
    sneaky = await client.post(
        f"{API}/auth/register", json=register_payload(is_verified_organizer=True)
    )
    assert admin.status_code == 422
    assert admin.json()["error"]["code"] == "VALIDATION_ERROR"
    assert sneaky.status_code == 422


async def test_uppercase_email_is_normalised_and_login_is_case_insensitive(
    client: httpx.AsyncClient, sql: Rows
) -> None:
    created = await client.post(
        f"{API}/auth/register", json=register_payload(email="Ada@Example.COM")
    )
    assert created.status_code == 201
    assert created.json()["email"] == "ada@example.com"
    assert (await sql("SELECT email FROM users"))[0][0] == "ada@example.com"
    login = await client.post(
        f"{API}/auth/login", json={"email": "ADA@example.com", "password": "correct-horse-battery"}
    )
    assert login.status_code == 200


async def test_session_cookie_is_httponly_samesite_lax(client: httpx.AsyncClient) -> None:
    response = await client.post(f"{API}/auth/register", json=register_payload())
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "secure" not in cookie.replace("samesite", "")


async def test_duplicate_email_and_bad_credentials_are_rejected(client: httpx.AsyncClient) -> None:
    await client.post(f"{API}/auth/register", json=register_payload())
    duplicate = await client.post(f"{API}/auth/register", json=register_payload())
    wrong = await client.post(
        f"{API}/auth/login", json={"email": "ada@example.com", "password": "wrong-password"}
    )
    unknown = await client.post(
        f"{API}/auth/login", json={"email": "ghost@example.com", "password": "wrong-password"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_foreign_or_missing_origin_is_rejected_but_referer_and_reads_are_not(
    app: FastAPI, make_client: Callable[..., Awaitable[httpx.AsyncClient]]
) -> None:
    bad_login = {"email": "ghost@example.com", "password": "wrong-password"}
    evil = await make_client(app, origin="https://evil.example")
    bare = await make_client(app, origin=None)
    rejected = [
        await evil.post(f"{API}/auth/login", json=bad_login),
        await bare.post(f"{API}/auth/login", json=bad_login),
    ]
    via_referer = await bare.post(
        f"{API}/auth/login", json=bad_login, headers={"Referer": "http://localhost:5173/events"}
    )
    read = await evil.get("/healthz")
    assert [r.status_code for r in rejected] == [403, 403]
    assert rejected[0].json()["error"]["code"] == "CSRF_ORIGIN_REJECTED"
    assert via_referer.status_code == 401  # passed the guard, failed on credentials
    assert read.status_code == 200


async def test_only_a_verified_organizer_or_admin_can_create_events(
    client: httpx.AsyncClient, create_user: Callable[..., Awaitable[Actor]]
) -> None:
    attendee = await create_user("ATTENDEE")
    organizer = await create_user("ORGANIZER", verified=False)
    admin = await create_user("SUPER_ADMIN")

    as_attendee = await client.post(f"{API}/events", json=event_payload(), headers=attendee.headers)
    unverified = await client.post(f"{API}/events", json=event_payload(), headers=organizer.headers)
    verify = await client.patch(
        f"{API}/admin/organizers/{organizer.id}/verify",
        json={"is_verified": True},
        headers=admin.headers,
    )
    created = await client.post(f"{API}/events", json=event_payload(), headers=organizer.headers)

    assert as_attendee.status_code == 403
    assert unverified.status_code == 403
    assert unverified.json()["error"]["code"] == "ORGANIZER_NOT_VERIFIED"
    assert verify.status_code == 200
    assert created.status_code == 201
    assert created.json()["organizer_id"] == str(organizer.id)


async def test_non_admins_cannot_use_admin_routes(
    client: httpx.AsyncClient, create_user: Callable[..., Awaitable[Actor]]
) -> None:
    organizer = await create_user("ORGANIZER", verified=True)
    response = await client.get(f"{API}/admin/organizers", headers=organizer.headers)
    assert response.status_code == 403


async def test_event_creation_seeds_the_full_seat_grid_atomically(
    client: httpx.AsyncClient, create_user: Callable[..., Awaitable[Actor]], sql: Rows
) -> None:
    organizer = await create_user("ORGANIZER", verified=True)
    response = await client.post(
        f"{API}/events", json=event_payload(rows=3, cols=5), headers=organizer.headers
    )
    labels = [
        r[0] for r in await sql("SELECT seat_label FROM seats ORDER BY row_label, seat_number")
    ]
    assert response.status_code == 201
    assert len(labels) == 15
    assert labels[0] == "A1"
    assert labels[-1] == "C5"


@pytest.mark.parametrize(
    "overrides",
    [
        {"rows": 11},
        {"cols": 13},
        {"starts_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
        {"booking_closes_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
        {"booking_closes_at": (datetime.now(UTC) + timedelta(days=31)).isoformat()},
        {"starts_at": "2030-01-01T00:00:00"},
    ],
)
async def test_invalid_event_payloads_are_rejected_with_422(
    client: httpx.AsyncClient,
    create_user: Callable[..., Awaitable[Actor]],
    overrides: dict[str, object],
) -> None:
    organizer = await create_user("ORGANIZER", verified=True)
    response = await client.post(
        f"{API}/events", json=event_payload(**overrides), headers=organizer.headers
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_authorisation_failures_on_foreign_bookings_never_wait_for_locks(
    client: httpx.AsyncClient,
    session_factory: SessionFactory,
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
) -> None:
    owner = await create_user()
    intruder = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    booking_id = (await hold(owner, event, ["A1"])).json()["booking_id"]

    async def attempt(action: str, extra: dict[str, object]) -> httpx.Response:
        return await client.post(
            f"{API}/bookings/{booking_id}/{action}",
            json=extra,
            headers={**intruder.headers, "Idempotency-Key": str(uuid.uuid4())},
        )

    blocker = session_factory()
    async with blocker, blocker.begin():
        # Hold every lock a locking path would need. A 403 that waited would time out below.
        await blocker.execute(
            text("SELECT id FROM bookings WHERE id = CAST(:b AS uuid) FOR UPDATE"),
            {"b": booking_id},
        )
        await advisory_xact_lock(blocker, advisory_lock_key("hoard", str(owner.id), str(event.id)))
        confirm, cancel = await asyncio.wait_for(
            asyncio.gather(
                attempt("confirm", {"simulate_outcome": "SUCCESS"}), attempt("cancel", {})
            ),
            timeout=5,
        )
    assert confirm.status_code == cancel.status_code == 403
    assert confirm.json()["error"]["code"] == "FORBIDDEN"


async def test_duplicate_seat_ids_and_missing_idempotency_key_are_422(
    client: httpx.AsyncClient,
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
) -> None:
    attendee = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    seat = str(event.seats["A1"])
    body = {"event_id": str(event.id), "seat_ids": [seat, seat]}
    duplicate = await client.post(
        f"{API}/bookings/hold", json=body, headers={**attendee.headers, "Idempotency-Key": "k1"}
    )
    no_key = await client.post(
        f"{API}/bookings/hold",
        json={"event_id": str(event.id), "seat_ids": [seat]},
        headers=attendee.headers,
    )
    assert duplicate.status_code == no_key.status_code == 422


async def test_seat_from_another_event_is_a_404_seat_not_found(
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
) -> None:
    attendee = await create_user()
    organizer = await create_user("ORGANIZER", verified=True)
    event_a = await create_event(organizer.id)
    event_b = await create_event(organizer.id)
    response = await hold(attendee, EventFx(event_a.id, event_b.seats), ["A1"])
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SEAT_NOT_FOUND"


async def test_hold_after_booking_cutoff_is_409_booking_closed(
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    sql: Rows,
) -> None:
    attendee = await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    await sql(
        "UPDATE events SET booking_closes_at = statement_timestamp() - interval '1 minute'"
        " WHERE id = :id",
        id=event.id,
    )
    response = await hold(attendee, event, ["A1"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "BOOKING_CLOSED"


async def test_demo_login_works_but_demo_accounts_have_no_usable_password(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    await run_seed(session_factory)
    demo = await client.post(f"{API}/auth/demo-login", json={"persona": "SUPER_ADMIN"})
    password = await client.post(
        f"{API}/auth/login", json={"email": "admin@eventseat.demo", "password": "password123"}
    )
    assert demo.status_code == 200
    assert demo.json()["role"] == "SUPER_ADMIN"
    assert password.status_code == 401


async def test_demo_login_is_404_when_demo_tools_are_disabled(
    make_app: Callable[..., Awaitable[FastAPI]],
    make_client: Callable[..., Awaitable[httpx.AsyncClient]],
) -> None:
    client = await make_client(await make_app(enable_demo_tools=False))
    response = await client.post(f"{API}/auth/demo-login", json={"persona": "ATTENDEE"})
    assert response.status_code == 404


# --- T12 reads ------------------------------------------------------------------------------


async def test_catalog_filters_paginates_and_reports_live_counts(
    client: httpx.AsyncClient,
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
) -> None:
    organizer = await create_user("ORGANIZER", verified=True)
    attendee = await create_user()
    talk = await create_event(organizer.id, title="Async Python Talk", category="CONFERENCE")
    await create_event(organizer.id, title="Postgres Workshop", category="WORKSHOP", cols=2)
    await create_event(
        organizer.id, title="Go Meetup", category="MEETUP", starts_in=timedelta(days=60)
    )
    await hold(attendee, talk, ["A1", "A2"])

    everything = (await client.get(f"{API}/events")).json()
    search = (await client.get(f"{API}/events", params={"q": "postgres"})).json()
    wildcard = (await client.get(f"{API}/events", params={"q": "%"})).json()
    workshops = (await client.get(f"{API}/events", params={"category": "WORKSHOP"})).json()
    page_two = (await client.get(f"{API}/events", params={"page": 2, "page_size": 2})).json()

    by_title = {item["title"]: item for item in everything["items"]}
    assert everything["total"] == 3
    assert by_title["Async Python Talk"]["total_seats"] == 8
    assert by_title["Async Python Talk"]["available_seats"] == 6
    assert by_title["Postgres Workshop"]["available_seats"] == 4
    assert [i["title"] for i in search["items"]] == ["Postgres Workshop"]
    assert wildcard["items"] == []
    assert [i["title"] for i in workshops["items"]] == ["Postgres Workshop"]
    assert (page_two["page"], len(page_two["items"]), page_two["total"]) == (2, 1, 3)
    assert everything["items"][-1]["title"] == "Go Meetup"


async def test_seat_map_distinguishes_available_held_booked_and_mine(
    client: httpx.AsyncClient,
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    confirm: Callable[..., Awaitable[httpx.Response]],
) -> None:
    alice, bob = await create_user(), await create_user()
    event = await create_event((await create_user("ORGANIZER", verified=True)).id)
    await hold(alice, event, ["A1"])
    bob_booking = (await hold(bob, event, ["A2"])).json()["booking_id"]
    await confirm(bob, bob_booking)

    async def statuses(headers: dict[str, str]) -> dict[str, str]:
        response = await client.get(f"{API}/events/{event.id}/seats", headers=headers)
        assert response.status_code == 200
        return {s["seat_label"]: s["status"] for s in response.json()["seats"]}

    assert (await statuses(alice.headers)) | {} == {
        "A1": "MINE", "A2": "BOOKED", "A3": "AVAILABLE", "A4": "AVAILABLE",
        "B1": "AVAILABLE", "B2": "AVAILABLE", "B3": "AVAILABLE", "B4": "AVAILABLE",
    }  # fmt: skip
    as_bob = await statuses(bob.headers)
    assert (as_bob["A1"], as_bob["A2"]) == ("HELD", "MINE")
    anonymous = await statuses({})
    assert (anonymous["A1"], anonymous["A2"]) == ("HELD", "BOOKED")
    missing = await client.get(f"{API}/events/{uuid.uuid4()}/seats")
    assert missing.status_code == 404


async def test_analytics_numbers_and_organizer_only_access(
    client: httpx.AsyncClient,
    create_user: Callable[..., Awaitable[Actor]],
    create_event: Callable[..., Awaitable[EventFx]],
    hold: Callable[..., Awaitable[httpx.Response]],
    confirm: Callable[..., Awaitable[httpx.Response]],
) -> None:
    organizer = await create_user("ORGANIZER", verified=True)
    rival = await create_user("ORGANIZER", verified=True)
    attendee, other = await create_user(), await create_user()
    event = await create_event(organizer.id)
    booking_id = (await hold(attendee, event, ["A1"])).json()["booking_id"]
    await confirm(attendee, booking_id)
    await hold(other, event, ["B1"])

    mine = await client.get(f"{API}/events/{event.id}/analytics", headers=organizer.headers)
    rivals = await client.get(f"{API}/events/{event.id}/analytics", headers=rival.headers)
    attendees = await client.get(f"{API}/events/{event.id}/analytics", headers=attendee.headers)
    missing = await client.get(f"{API}/events/{uuid.uuid4()}/analytics", headers=organizer.headers)

    body = mine.json()
    assert (body["total_seats"], body["confirmed"], body["held"], body["available"]) == (8, 1, 1, 6)
    assert body["utilization_pct"] == 12.5
    assert [e["seat_labels"] for e in body["roster"]] == [["A1"]]
    assert rivals.status_code == attendees.status_code == 403
    assert missing.status_code == 404
