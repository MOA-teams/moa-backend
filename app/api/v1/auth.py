from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.deps.auth import get_user
from app.deps.db import get_db
from app.schemas.auth import AuthenticatedUser, LoginRequest, RefreshRequest, SignupRequest, SignupResponse, TokenResponse
from app.services.auth import (
    EmailAlreadyExistsError, InvalidCredentialsError, InvalidRefreshTokenError,
    login, refresh_tokens, signup,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/signup",
    status_code=status.HTTP_201_CREATED,
    response_model=SignupResponse,
    responses={
        400: {"description": "입력 검증 실패"},
        409: {"description": "이미 등록된 이메일"},
        "4XX": {"description": "공통 오류 응답"},
    },
)
async def signup_user(
    payload: SignupRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> SignupResponse:
    try:
        user = await signup(
            session, email=payload.email,
            password=payload.password.get_secret_value(), role=payload.role,
        )
    except EmailAlreadyExistsError:
        raise APIError(409, "EMAIL_ALREADY_EXISTS", "이미 등록된 이메일입니다.") from None
    return SignupResponse.model_validate(user)


@router.post(
    "/login", response_model=TokenResponse,
    responses={400: {"description": "입력 검증 실패"}, 401: {"description": "인증 실패"}, "4XX": {"description": "공통 오류 응답"}},
)
async def login_user(
    payload: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    try:
        pair = await login(session, email=payload.email, password=payload.password.get_secret_value())
    except InvalidCredentialsError:
        raise APIError(401, "INVALID_CREDENTIALS", "이메일 또는 비밀번호가 올바르지 않습니다.") from None
    return TokenResponse.model_validate(pair)


@router.post(
    "/refresh", response_model=TokenResponse,
    responses={400: {"description": "입력 검증 실패"}, 401: {"description": "갱신 토큰 검증 실패"}, "4XX": {"description": "공통 오류 응답"}},
)
async def refresh_user_tokens(
    payload: RefreshRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    try:
        pair = await refresh_tokens(session, refresh_token=payload.refresh_token.get_secret_value())
    except InvalidRefreshTokenError:
        raise APIError(401, "INVALID_REFRESH_TOKEN", "유효한 갱신 토큰이 필요합니다.") from None
    return TokenResponse.model_validate(pair)


@router.get(
    "/me", response_model=AuthenticatedUser,
    responses={401: {"description": "인증 실패"}, "4XX": {"description": "공통 오류 응답"}},
)
async def get_my_info(
    user: Annotated[AuthenticatedUser, Depends(get_user)],
) -> AuthenticatedUser:
    return user
