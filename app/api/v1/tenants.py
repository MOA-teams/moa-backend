from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserRole
from app.core.errors import APIError
from app.deps.auth import authentication_error, get_user, require_roles
from app.deps.db import get_db
from app.schemas.auth import AuthenticatedUser
from app.schemas.tenants import TenantCreateRequest, TenantJoinRequest, TenantResponse
from app.services.tenants import (
    TenantAlreadyAssignedError, TenantAlreadyExistsError, TenantError,
    TenantNotFoundError, TenantPermissionDeniedError, TenantRequiredError,
    UserNotFoundError, create_tenant, get_my_tenant, join_tenant,
)

router = APIRouter(prefix="/tenants", tags=["tenants"])

ERRORS = {
    TenantAlreadyExistsError: (409, "TENANT_ALREADY_EXISTS", "이미 등록된 기업 ID입니다."),
    TenantAlreadyAssignedError: (409, "TENANT_ALREADY_ASSIGNED", "이미 기업에 소속되어 있습니다."),
    TenantNotFoundError: (404, "TENANT_NOT_FOUND", "기업을 찾을 수 없습니다."),
    TenantRequiredError: (403, "TENANT_REQUIRED", "기업 소속이 필요합니다."),
    TenantPermissionDeniedError: (403, "FORBIDDEN", "접근 권한이 없습니다."),
}


def tenant_api_error(error: TenantError) -> APIError:
    if isinstance(error, UserNotFoundError):
        return authentication_error()
    return APIError(*ERRORS[type(error)])


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=TenantResponse,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
    responses={400: {"description": "입력 오류"}, 401: {"description": "인증 실패"}, 403: {"description": "역할 제한"}, 409: {"description": "기업 또는 소속 충돌"}, "4XX": {"description": "공통 오류 응답"}},
)
async def create_company(
    payload: TenantCreateRequest,
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TenantResponse:
    try:
        tenant = await create_tenant(session, user_id=user.user_id, tenant_id=payload.tenant_id, name=payload.name)
    except TenantError as error:
        raise tenant_api_error(error) from None
    return TenantResponse.model_validate(tenant)


@router.post(
    "/join", response_model=TenantResponse,
    responses={400: {"description": "입력 오류"}, 401: {"description": "인증 실패"}, 404: {"description": "기업 없음"}, 409: {"description": "소속 충돌"}, "4XX": {"description": "공통 오류 응답"}},
)
async def join_company(
    payload: TenantJoinRequest,
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TenantResponse:
    try:
        tenant = await join_tenant(session, user_id=user.user_id, tenant_id=payload.tenant_id)
    except TenantError as error:
        raise tenant_api_error(error) from None
    return TenantResponse.model_validate(tenant)


@router.get(
    "/me", response_model=TenantResponse,
    responses={401: {"description": "인증 실패"}, 403: {"description": "기업 소속 필요"}, "4XX": {"description": "공통 오류 응답"}},
)
async def get_my_company(
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TenantResponse:
    try:
        tenant = await get_my_tenant(session, user_id=user.user_id)
    except TenantError as error:
        raise tenant_api_error(error) from None
    return TenantResponse.model_validate(tenant)
