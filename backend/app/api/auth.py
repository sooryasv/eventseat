"""Auth routes: thin handlers that parse, call a service, and return a model."""

from fastapi import APIRouter, Depends, Response

from app.api.deps import AppSettings, CurrentUser, DbSession, require_demo_tools
from app.schemas import DemoLoginRequest, LoginRequest, OkOut, RegisterRequest, UserOut
from app.security import clear_auth_cookie, create_access_token, set_auth_cookie
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201, response_model=UserOut)
async def register(
    payload: RegisterRequest, response: Response, db: DbSession, settings: AppSettings
) -> UserOut:
    user = await auth_service.register_user(db, payload)
    set_auth_cookie(response, create_access_token(user.id, settings), settings)
    return user


@router.post("/login", response_model=UserOut)
async def login(
    payload: LoginRequest, response: Response, db: DbSession, settings: AppSettings
) -> UserOut:
    user = await auth_service.authenticate(db, payload.email, payload.password)
    set_auth_cookie(response, create_access_token(user.id, settings), settings)
    return user


@router.post("/demo-login", response_model=UserOut, dependencies=[Depends(require_demo_tools)])
async def demo_login(
    payload: DemoLoginRequest, response: Response, db: DbSession, settings: AppSettings
) -> UserOut:
    user = await auth_service.demo_login(db, payload.persona)
    set_auth_cookie(response, create_access_token(user.id, settings), settings)
    return user


@router.post("/logout", response_model=OkOut)
async def logout(response: Response, _user: CurrentUser, settings: AppSettings) -> OkOut:
    clear_auth_cookie(response, settings)
    return OkOut()


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return user
