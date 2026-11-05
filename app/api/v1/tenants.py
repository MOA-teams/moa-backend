from typing import Annotated

from fastapi import APIRouter, Body, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.documentation import (
    TENANT_CREATE_EXAMPLES, TENANT_EXAMPLE, TENANT_JOIN_EXAMPLES,
    documented_errors, documented_success,
)
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
    summary="기업 생성 및 본인 소속 연결",
    description="""
**기업 미소속 admin 전용** API입니다. Bearer access token이 필요합니다.

- 기업 ID는 영문 소문자 a~z만 1~64자 사용합니다.
- 기업 표시 이름은 앞뒤 공백을 제거한 뒤 1~100자여야 합니다.
- 기업 생성과 생성한 관리자 본인의 소속 연결은 함께 성공하거나 함께 취소됩니다.
- 이미 기업에 소속된 관리자는 새 기업을 생성할 수 없습니다 (`409 TENANT_ALREADY_ASSIGNED`).
- 기업 ID 중복은 `409 TENANT_ALREADY_EXISTS`입니다.
- 성공 후 내 정보 API에서 소속을 확인하세요. 사용자 ID나 역할을 본문에 보내지 않습니다.
""",
    response_description="생성한 기업. 요청한 관리자도 이 기업에 소속됨",
    responses={
        **documented_errors("VALIDATION_ERROR", "INVALID_ACCESS_TOKEN", "FORBIDDEN", "TENANT_ALREADY_EXISTS", "TENANT_ALREADY_ASSIGNED", bearer_auth=True),
        201: documented_success("기업 생성과 관리자 소속 연결 완료", {
            "created": {"summary": "싱크노트 생성", "value": TENANT_EXAMPLE},
        }),
    },
)
async def create_company(
    payload: Annotated[TenantCreateRequest, Body(openapi_examples=TENANT_CREATE_EXAMPLES)],
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TenantResponse:
    try:
        tenant = await create_tenant(session, user_id=user.internal_user_id, tenant_id=payload.tenant_id, name=payload.name)
    except TenantError as error:
        raise tenant_api_error(error) from None
    return TenantResponse.model_validate(tenant)


@router.post(
    "/join", response_model=TenantResponse,
    summary="본인의 기업 소속 연결",
    description="""
**customer·agent·admin 모두 사용 가능**하며 Bearer access token이 필요합니다.

- 연결할 기존 기업의 `tenant_id`만 보냅니다. 소속을 연결할 계정은 토큰으로 결정합니다.
- 현재 정책에서는 기업 ID로 자유 연결하며 초대·승인 절차는 없습니다.
- 같은 기업에 재요청하면 `200`으로 현재 기업을 반환합니다.
- 이미 다른 기업에 속한 경우 `409 TENANT_ALREADY_ASSIGNED`이며 기업을 이동시키지 않습니다.
- 미소속 사용자가 존재하지 않는 기업을 지정하면 `404 TENANT_NOT_FOUND`입니다.
- 연결 후 기존 access token으로 내 정보·내 기업을 다시 조회할 수 있습니다.
""",
    response_description="연결된 기업. 동일 기업 재연결도 같은 성공 응답",
    responses={
        **documented_errors("VALIDATION_ERROR", "INVALID_ACCESS_TOKEN", "TENANT_NOT_FOUND", "TENANT_ALREADY_ASSIGNED", bearer_auth=True),
        200: documented_success("소속 연결 완료 또는 이미 같은 기업에 소속됨", {
            "joined": {"summary": "싱크노트 소속", "value": TENANT_EXAMPLE},
        }),
    },
)
async def join_company(
    payload: Annotated[TenantJoinRequest, Body(openapi_examples=TENANT_JOIN_EXAMPLES)],
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TenantResponse:
    try:
        tenant = await join_tenant(session, user_id=user.internal_user_id, tenant_id=payload.tenant_id)
    except TenantError as error:
        raise tenant_api_error(error) from None
    return TenantResponse.model_validate(tenant)


@router.get(
    "/me", response_model=TenantResponse,
    summary="내 기업 조회",
    description="""
**기업에 소속된 인증 사용자**의 기업 정보를 조회합니다. 세 역할 모두 사용할 수 있습니다.

- Bearer access token이 필요합니다.
- 기업 ID를 요청에 보내지 않습니다. 서버가 현재 계정의 DB 소속으로 조회합니다.
- 미소속 계정이면 `403 TENANT_REQUIRED`입니다. 기업 생성 또는 소속 연결을 먼저 진행하세요.
- 역할 문제와 미소속 문제는 토큰 갱신으로 해결되지 않습니다. 내 정보의 역할·소속을 확인하세요.
""",
    response_description="현재 계정이 소속된 기업 정보",
    responses={
        **documented_errors("INVALID_ACCESS_TOKEN", "TENANT_REQUIRED", "TENANT_NOT_FOUND", bearer_auth=True),
        200: documented_success("본인 소속 기업 조회 완료", {
            "company": {"summary": "현재 기업", "value": TENANT_EXAMPLE},
        }),
    },
)
async def get_my_company(
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TenantResponse:
    try:
        tenant = await get_my_tenant(session, user_id=user.internal_user_id)
    except TenantError as error:
        raise tenant_api_error(error) from None
    return TenantResponse.model_validate(tenant)
