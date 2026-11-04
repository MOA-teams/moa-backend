from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.security import InvalidAccessTokenError, decode_access_token
from app.db.models import User
from app.deps.db import get_db
from app.schemas.auth import AuthenticatedUser

bearer = HTTPBearer(auto_error=False)


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
    user = await session.get(User, claims.user_id)
    if user is None or user.role != claims.role:
        raise authentication_error()
    return AuthenticatedUser.model_validate(user)
