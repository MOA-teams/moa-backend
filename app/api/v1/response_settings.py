from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserRole
from app.core.errors import APIError
from app.deps.auth import require_roles
from app.deps.db import get_db
from app.schemas.auth import AuthenticatedUser
from app.schemas.response_settings import ResponseSettingsUpdate, ResponseSettingsView
from app.services.response_settings import settings_view, update_settings

router = APIRouter(
    prefix="/admin/settings/auto-response", tags=["auto-response-settings"]
)
Reader = Annotated[
    AuthenticatedUser, Depends(require_roles(UserRole.ADMIN, UserRole.AGENT))
]
Admin = Annotated[AuthenticatedUser, Depends(require_roles(UserRole.ADMIN))]
DB = Annotated[AsyncSession, Depends(get_db)]


def check_tenant(user):
    if not user.tenant_id:
        raise APIError(403, "TENANT_REQUIRED", "기업 소속이 필요합니다.")


@router.get("", response_model=ResponseSettingsView, summary="자동 질문·답변 설정 조회")
async def get_settings(user: Reader, session: DB):
    check_tenant(user)
    return await settings_view(session, user.tenant_id)


@router.patch(
    "", response_model=ResponseSettingsView, summary="관리자의 자동 질문·답변 설정 변경"
)
async def patch_settings(payload: ResponseSettingsUpdate, user: Admin, session: DB):
    check_tenant(user)
    return await update_settings(session, user, payload)
