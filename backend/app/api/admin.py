"""Super-admin routes: organizer verification."""

from uuid import UUID

from fastapi import APIRouter

from app.api.deps import DbSession, SuperAdmin
from app.schemas import OrganizerVerifyRequest, UserOut
from app.services import auth_service

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/organizers", response_model=list[UserOut])
async def list_organizers(_admin: SuperAdmin, db: DbSession) -> list[UserOut]:
    return await auth_service.list_organizers(db)


@router.patch("/organizers/{user_id}/verify", response_model=UserOut)
async def verify_organizer(
    user_id: UUID, payload: OrganizerVerifyRequest, _admin: SuperAdmin, db: DbSession
) -> UserOut:
    return await auth_service.set_organizer_verified(db, user_id, payload.is_verified)
