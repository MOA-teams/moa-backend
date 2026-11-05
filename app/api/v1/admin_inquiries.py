from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.documentation import documented_errors
from app.api.v1.inquiries import Cursor, InquiryId, Limit
from app.core.enums import InquiryCategory, InquiryStatus, InquiryUrgency, UserRole
from app.deps.auth import get_user, require_roles
from app.deps.db import get_db
from app.schemas.auth import AuthenticatedUser
from app.schemas.inquiries import InquiryDetailResponse, InquiryListResponse
from app.services.admin_inquiries import get_inquiry_detail, list_inquiries

router = APIRouter(prefix="/admin/inquiries", tags=["admin-inquiries"], dependencies=[Depends(require_roles(UserRole.AGENT, UserRole.ADMIN))])
ERRORS = ("VALIDATION_ERROR", "INVALID_ACCESS_TOKEN", "FORBIDDEN", "TENANT_REQUIRED", "INVALID_CURSOR")


@router.get(
    "", response_model=InquiryListResponse,
    summary="본인 기업의 문의 목록 조회",
    description="""기업 소속 agent/admin 전용이며 다른 기업 문의는 목록에 포함하지 않습니다.
생성 시각 내림차순, 동일 시각은 숫자 문의 ID 내림차순입니다. 기본 20개, 최대 100개입니다.
status/category/urgency는 한글 Enum입니다. category는 고객 선택 분류가 아닌 AI 분류를 필터링합니다.
아직 AI 분석이 없으면 category·urgency·processing_stage·group_id는 null이며 해당 분류 필터에 매칭되지 않습니다.
필터 변경 시 cursor를 비우고 처음부터 조회하세요. 마지막 페이지의 next_cursor는 null입니다.
처음부터 새로 조회하면 이후 접수된 최신 문의를 볼 수 있습니다.""",
    responses=documented_errors(*ERRORS, bearer_auth=True),
)
async def listing(
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    cursor: Cursor = None, limit: Limit = 20,
    status: Annotated[InquiryStatus | None, Query(description="문의 상태, 예: 접수")] = None,
    category: Annotated[InquiryCategory | None, Query(description="AI 분류, 예: 기능 오류. 미분석 문의는 null")] = None,
    urgency: Annotated[InquiryUrgency | None, Query(description="긴급도, 예: 높음. 미분석 문의는 null")] = None,
) -> InquiryListResponse:
    return await list_inquiries(session, user=user, cursor=cursor, limit=limit, status=status, category=category, urgency=urgency)


@router.get(
    "/{inquiry_id}", response_model=InquiryDetailResponse,
    summary="본인 기업의 문의 상세와 대화 조회",
    description="""agent/admin이 본인 기업의 문의만 조회합니다. 다른 기업의 문의는 404입니다.
원문 대화·고객 선택 분류·환경·revision·최신 고객 메시지 ID를 반환합니다.
source_customer_id는 싱크노트 등 외부에서 전달받은 참고용 식별 문자열이며, 실제 인증 고객은 customer_id입니다.
기존 문의 또는 외부 ID 없이 접수한 문의의 source_customer_id는 null입니다. 이 필드는 담당자 상세에만 제공합니다.
메시지는 고객 조회와 같은 cursor 규칙이며 최초 생략, 이후 next_cursor로 폴링합니다.
아직 구현하지 않은 AI 실행·초안·그룹·검수 필드는 null입니다. 조회만으로 상태를 변경하지 않습니다.
첨부 다운로드는 각 attachment_id로 /attachments/{id}를 호출합니다.""",
    responses=documented_errors(*ERRORS, "INQUIRY_NOT_FOUND", bearer_auth=True),
)
async def detail(
    inquiry_id: InquiryId,
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    cursor: Cursor = None, limit: Limit = 20,
) -> InquiryDetailResponse:
    return await get_inquiry_detail(session, user=user, inquiry_id=inquiry_id, cursor=cursor, limit=limit)
