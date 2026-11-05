from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserRole
from app.core.errors import APIError
from app.core.security import InvalidAccessTokenError, decode_access_token
from app.db.models import User
from app.deps.db import get_db
from app.schemas.auth import AuthenticatedUser

bearer = HTTPBearer(
    auto_error=False, bearerFormat="JWT",
    description="로그인 응답의 access_token 값만 입력하세요. Swagger가 Bearer 접두어를 붙입니다. refresh_token은 /api/v1/auth/refresh의 JSON 본문에 사용합니다.",
)


def authentication_error() -> APIError:
    return APIError(
        401, "INVALID_ACCESS_TOKEN", "유효한 인증 정보가 필요합니다.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AuthenticatedUser:
    if credentials is None:
        raise authentication_error()
    try:
        claims = decode_access_token(credentials.credentials)
    except InvalidAccessTokenError:
        raise authentication_error() from None
    user = await session.scalar(select(User).where(User.public_id == claims.user_id))
    if user is None or user.role != claims.role:
        raise authentication_error()
    return AuthenticatedUser.model_validate(user)


def require_roles(*roles: UserRole) -> Callable[..., Awaitable[AuthenticatedUser]]:
    if not roles:
        raise ValueError("허용할 역할을 하나 이상 지정해야 합니다.")
    if any(not isinstance(role, UserRole) for role in roles):
        raise TypeError("역할은 UserRole 값으로 지정해야 합니다.")
    allowed_roles = frozenset(roles)

    async def check_roles(
        user: Annotated[AuthenticatedUser, Depends(get_user)],
    ) -> AuthenticatedUser:
        if user.role not in allowed_roles:
            raise APIError(403, "FORBIDDEN", "접근 권한이 없습니다.")
        return user

    return check_roles
