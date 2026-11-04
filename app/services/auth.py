from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.enums import UserRole
from app.core.security import hash_password
from app.db.models import User


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
