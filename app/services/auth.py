from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.enums import UserRole
from app.core.security import (
    DUMMY_PASSWORD_HASH, create_access_token, create_refresh_token,
    hash_password, hash_refresh_token, verify_password,
)
from app.db.models import RefreshToken, User


class EmailAlreadyExistsError(Exception):
    pass


async def signup(
    session: AsyncSession, *, email: str, password: str, role: UserRole,
) -> User:
    hashed_password = await run_in_threadpool(hash_password, password)
    user = User(email=email, password=hashed_password, role=role)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        cause = error.orig.__cause__
        if (
            getattr(error.orig, "sqlstate", None) == "23505"
            and getattr(cause, "constraint_name", None) == "uq_users_email"
        ):
            raise EmailAlreadyExistsError from None
        raise
    return user


class InvalidCredentialsError(Exception):
    pass


class InvalidRefreshTokenError(Exception):
    pass


@dataclass(frozen=True)
class TokenPair:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)


def prepare_token_pair(session: AsyncSession, user: User) -> TokenPair:
    refresh_token = create_refresh_token()
    pair = TokenPair(
        access_token=create_access_token(user.user_id, user.role),
        refresh_token=refresh_token,
    )
    session.add(RefreshToken(
        user_id=user.user_id,
        token_hash=hash_refresh_token(refresh_token),
        expires_at=datetime.now(timezone.utc) + timedelta(days=get_settings().refresh_token_expire_days),
    ))
    return pair


async def login(session: AsyncSession, *, email: str, password: str) -> TokenPair:
    user = await session.scalar(select(User).where(User.email == email))
    stored_hash = user.password if user is not None else DUMMY_PASSWORD_HASH
    valid = await run_in_threadpool(verify_password, password, stored_hash)
    if user is None or not valid:
        raise InvalidCredentialsError
    try:
        pair = prepare_token_pair(session, user)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return pair


async def refresh_tokens(session: AsyncSession, *, refresh_token: str) -> TokenPair:
    try:
        token = await session.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == hash_refresh_token(refresh_token))
            .with_for_update()
        )
        # 잠금을 기다리는 동안 만료될 수 있으므로 잠금 획득 후 시각을 검사한다.
        if token is None or token.expires_at <= datetime.now(timezone.utc):
            raise InvalidRefreshTokenError
        user = await session.get(User, token.user_id)
        if user is None:
            raise InvalidRefreshTokenError
        await session.delete(token)
        pair = prepare_token_pair(session, user)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return pair
