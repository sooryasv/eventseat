"""Registration, login, demo personas, and organizer verification."""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFound, Unauthenticated, map_integrity_error
from app.models import User
from app.schemas import RegisterRequest, Role, UserOut
from app.security import hash_password, verify_password

DEMO_PERSONA_EMAILS: dict[Role, str] = {
    "ATTENDEE": "attendee@eventseat.demo",
    "ORGANIZER": "organizer@eventseat.demo",
    "SUPER_ADMIN": "admin@eventseat.demo",
}


async def register_user(session: AsyncSession, data: RegisterRequest) -> UserOut:
    password_hash = await hash_password(data.password)
    # Never verified at signup: only a SUPER_ADMIN can flip is_verified_organizer.
    user = User(
        email=data.email,
        full_name=data.full_name,
        password_hash=password_hash,
        role=data.role,
        is_verified_organizer=False,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        mapped = map_integrity_error(exc)
        if mapped is None:
            raise
        raise mapped from exc
    return UserOut.model_validate(user)


async def authenticate(session: AsyncSession, email: str, password: str) -> UserOut:
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    password_ok = await verify_password(password, user.password_hash if user else None)
    if user is None or not password_ok:
        raise Unauthenticated("Invalid email or password")
    return UserOut.model_validate(user)


async def get_user(session: AsyncSession, user_id: UUID) -> UserOut | None:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    return UserOut.model_validate(user) if user else None


async def demo_login(session: AsyncSession, persona: Role) -> UserOut:
    email = DEMO_PERSONA_EMAILS[persona]
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        raise NotFound("Demo accounts are not seeded")
    return UserOut.model_validate(user)


async def list_organizers(session: AsyncSession) -> list[UserOut]:
    stmt = select(User).where(User.role == "ORGANIZER").order_by(User.created_at, User.id)
    return [UserOut.model_validate(u) for u in (await session.scalars(stmt)).all()]


async def set_organizer_verified(
    session: AsyncSession, user_id: UUID, is_verified: bool
) -> UserOut:
    stmt = (
        update(User)
        .where(User.id == user_id, User.role == "ORGANIZER")
        .values(is_verified_organizer=is_verified)
        .returning(User)
    )
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise NotFound("Organizer not found")
    result = UserOut.model_validate(user)
    await session.commit()
    return result
