from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserRole
from app.db.models import Tenant, User


class TenantError(Exception):
    pass


class TenantAlreadyExistsError(TenantError):
    pass


class TenantAlreadyAssignedError(TenantError):
    pass


class TenantNotFoundError(TenantError):
    pass


class TenantRequiredError(TenantError):
    pass


class UserNotFoundError(TenantError):
    pass


class TenantPermissionDeniedError(TenantError):
    pass


async def lock_user(session: AsyncSession, user_id: int) -> User:
    # 인증 단계에서 읽은 값이 있더라도 잠금을 기다린 뒤의 최신 소속을 사용한다.
    user = await session.scalar(
        select(User).where(User.user_id == user_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise UserNotFoundError
    return user


async def create_tenant(
    session: AsyncSession, *, user_id: int, tenant_id: str, name: str,
) -> Tenant:
    try:
        user = await lock_user(session, user_id)
        # 실제 쓰기 직전에도 현재 역할을 확인해 인증 이후의 변경을 반영한다.
        if user.role != UserRole.ADMIN:
            raise TenantPermissionDeniedError
        if user.tenant_id is not None:
            raise TenantAlreadyAssignedError
        tenant = Tenant(tenant_id=tenant_id, name=name)
        session.add(tenant)
        await session.flush()
        user.tenant_id = tenant.tenant_id
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        if (
            getattr(error.orig, "sqlstate", None) == "23505"
            and getattr(error.orig.__cause__, "constraint_name", None) == "pk_tenants"
        ):
            raise TenantAlreadyExistsError from None
        raise
    except Exception:
        await session.rollback()
        raise
    return tenant


async def join_tenant(session: AsyncSession, *, user_id: int, tenant_id: str) -> Tenant:
    try:
        user = await lock_user(session, user_id)
        if user.tenant_id is not None and user.tenant_id != tenant_id:
            raise TenantAlreadyAssignedError
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise TenantNotFoundError
        if user.tenant_id is None:
            user.tenant_id = tenant.tenant_id
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return tenant


async def get_my_tenant(session: AsyncSession, *, user_id: int) -> Tenant:
    user = await session.get(User, user_id, populate_existing=True)
    if user is None:
        raise UserNotFoundError
    if user.tenant_id is None:
        raise TenantRequiredError
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise TenantNotFoundError
    return tenant
