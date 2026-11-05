from typing import Annotated
from fastapi import APIRouter, Depends, Header, Request
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.documentation import documented_errors
from app.api.v1.inquiries import IdempotencyKey
from app.core.config import get_settings
from app.core.errors import APIError
from app.core.github import GitHubError, get_github_client
from app.deps.auth import get_user
from app.deps.db import get_db
from app.schemas.analysis import Contract
from app.schemas.auth import AuthenticatedUser
from app.schemas.github import (
    AuthorizationComplete,
    GitHubConnect,
    GitHubAccepted,
    GitHubStatus,
    AuthorizationStart,
    AuthorizationResult,
    BranchSelection,
    PathSelection,
    WebhookAccepted,
)
from app.services import github as service

router = APIRouter(tags=["github"])
COMMON = documented_errors(
    "VALIDATION_ERROR",
    "INVALID_ACCESS_TOKEN",
    "FORBIDDEN",
    "TENANT_REQUIRED",
    "GITHUB_AUTHORIZATION_INVALID",
    "GITHUB_REPOSITORY_NOT_FOUND",
    "GITHUB_REPOSITORY_CONNECTED",
    "GITHUB_NOT_CONFIGURED",
    "GITHUB_UNAVAILABLE",
    bearer_auth=True,
)


class Selection(Contract):
    authorization_id: str = Field(min_length=32, max_length=200)
    repository_id: int = Field(strict=True, gt=0)
    branch: str | None = Field(default=None, min_length=1, max_length=200)


async def external(coro):
    try:
        return await coro
    except GitHubError as exc:
        status = 403 if exc.code == "GITHUB_ACCESS_DENIED" else 503
        raise APIError(
            status,
            exc.code,
            "GitHub 연결 설정 또는 접근 권한을 확인해 주세요. 인증 실패 시 연결을 다시 시작해 주세요.",
        ) from None


@router.post(
    "/admin/integrations/github/authorization",
    response_model=AuthorizationStart,
    summary="GitHub App 설치·사용자 인증 시작",
    responses=COMMON,
)
async def start(
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    github=Depends(get_github_client),
):
    return await external(service.start_authorization(session, user, github))


@router.post(
    "/admin/integrations/github/authorization/complete",
    response_model=AuthorizationResult,
    summary="인증 완료와 관리 가능한 설치·저장소 목록",
    responses=COMMON,
)
async def complete(
    body: AuthorizationComplete,
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    github=Depends(get_github_client),
):
    return await external(service.complete_authorization(session, user, body, github))


@router.post(
    "/admin/integrations/github/selection",
    response_model=BranchSelection | PathSelection,
    summary="인증한 저장소의 브랜치·경로 선택",
    responses=COMMON,
)
async def selection(
    body: Selection,
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    github=Depends(get_github_client),
):
    return await external(
        service.repository_selection(
            session,
            user,
            body.authorization_id,
            body.repository_id,
            github,
            body.branch,
        )
    )


@router.post(
    "/admin/integrations/github",
    status_code=202,
    response_model=GitHubAccepted,
    summary="GitHub 연동 설정·최초 인덱싱 접수",
    responses=COMMON,
    description="admin 전용. Idempotency-Key 필수. 레벨 0은 GitHub 없이 접수하며 기존 소스가 있으면 검색 차단합니다. 1은 메타데이터만, 2는 경로 제한 코드입니다. 3은 지원하지 않습니다. 연결 검증 후 worker가 실행하고 GET으로 진행 상태를 조회합니다.",
)
async def connect(
    body: GitHubConnect,
    key: IdempotencyKey,
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    github=Depends(get_github_client),
):
    return await external(service.connect(session, user, body, key, github))


@router.get(
    "/admin/integrations/github",
    response_model=GitHubStatus,
    summary="기업 GitHub 연동·필터·인덱싱 상태 조회",
    responses=COMMON,
)
async def status(
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    return await service.status(session, user)


@router.post(
    "/integrations/github/webhook",
    status_code=202,
    response_model=WebhookAccepted,
    summary="GitHub 서명 검증·중복 제거·영속 동기화 접수",
)
async def webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
    delivery: Annotated[str | None, Header(alias="X-GitHub-Delivery")] = None,
    event_type: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 2 * 1024 * 1024:
            raise APIError(
                413, "GITHUB_EVENT_TOO_LARGE", "Webhook 크기 제한을 초과했습니다."
            )
    secret = get_settings().github_webhook_secret
    return await service.webhook(
        session,
        raw=bytes(raw),
        signature=signature,
        delivery=delivery,
        event_type=event_type,
        secret=secret.get_secret_value() if secret else None,
    )
