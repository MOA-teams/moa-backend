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
분석 요청·run 상태·최신 유효 초안·검수 결과를 제공합니다. 새 메시지나 수동 수정으로 오래된 초안은 현재 초안에 표시하지 않습니다. group_id는 백엔드가 확정한 현재 그룹이며 미분석·충돌 시 null입니다. 조회만으로 상태를 변경하지 않습니다.
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


from app.api.v1.inquiries import IdempotencyKey
from app.schemas.analysis import AnalysisRetry, AnalysisAccepted, ClassificationRequest, ClassificationResponse
from app.services.analysis import request_analysis, classify

@router.post("/{inquiry_id}/analysis", status_code=202, response_model=AnalysisAccepted,
             responses=documented_errors("VALIDATION_ERROR","INVALID_ACCESS_TOKEN","FORBIDDEN","TENANT_REQUIRED","INQUIRY_NOT_FOUND","STALE_INQUIRY_REVISION","INQUIRY_CLOSED","IDEMPOTENCY_CONFLICT",bearer_auth=True),
             summary="최신 문의 재분석 요청",
             description="같은 기업 담당자 전용. expected_inquiry_revision과 based_on_message_id를 확인하고 요청을 DB에 보존합니다. AI 접수 전 run_id는 null이며 상세 조회로 진행 상황을 확인합니다. review_only는 기본 true이고 이번 단계는 고객에게 자동 발송하지 않습니다. Idempotency-Key 필수입니다.")
async def analyze(inquiry_id: InquiryId, payload: AnalysisRetry, key: IdempotencyKey,
                  user: Annotated[AuthenticatedUser, Depends(get_user)],session: Annotated[AsyncSession, Depends(get_db)]):
    return await request_analysis(session,user=user,inquiry_id=inquiry_id,payload=payload,key=key)

@router.patch("/{inquiry_id}/classification",response_model=ClassificationResponse,
              responses=documented_errors("VALIDATION_ERROR","INVALID_ACCESS_TOKEN","FORBIDDEN","TENANT_REQUIRED","INQUIRY_NOT_FOUND","STALE_INQUIRY_REVISION",bearer_auth=True),
              summary="담당자 분류·긴급도 확정",
              description="현재 expected_revision과 수정 사유를 보냅니다. 변경 이력을 저장하고 revision을 증가시킵니다. 이후 AI 결과는 수동 확정 값을 덮어쓰지 않습니다. revision 충돌은 409입니다.")
async def classification(inquiry_id: InquiryId,payload: ClassificationRequest,
                         user: Annotated[AuthenticatedUser, Depends(get_user)],session: Annotated[AsyncSession, Depends(get_db)]):
    return await classify(session,user=user,inquiry_id=inquiry_id,payload=payload)


from app.core.rag import get_rag_client
from app.schemas.answers import AnswerRequest, AnswerResponse, CloseRequest, CloseResponse
from app.services.answers import send_answer, close_inquiry

ANSWER_ERRORS = ("VALIDATION_ERROR", "INVALID_ACCESS_TOKEN", "FORBIDDEN", "TENANT_REQUIRED",
    "INQUIRY_NOT_FOUND", "INQUIRY_CLOSED", "IDEMPOTENCY_CONFLICT", "STALE_INQUIRY_REVISION",
    "STALE_CUSTOMER_MESSAGE", "STALE_DRAFT", "STALE_KNOWLEDGE", "STALE_EVIDENCE",
    "EVIDENCE_NOT_RETRIEVED", "CODE_EVIDENCE_UNVERIFIED", "DOCUMENT_NOT_FOUND",
    "EVIDENCE_CHECK_UNAVAILABLE", "ANSWER_ALREADY_SENT")


@router.post("/{inquiry_id}/answers", status_code=201, response_model=AnswerResponse,
    summary="담당자 최종 답변 승인·발송", responses=documented_errors(*ANSWER_ERRORS, bearer_auth=True),
    description="Idempotency-Key 필수. 상세 조회의 문의 revision·최신 고객 메시지·backend_knowledge_revision을 보냅니다. 초안 ID/revision은 함께 지정하거나 직접 작성 시 둘 다 null입니다. AI 검수 실패도 담당자 승인할 수 있지만 최신성 검사는 생략하지 않습니다. 답변과 채팅 메시지가 함께 저장된 뒤 201입니다.")
async def answer(inquiry_id: InquiryId, payload: AnswerRequest, key: IdempotencyKey,
    user: Annotated[AuthenticatedUser, Depends(get_user)], session: Annotated[AsyncSession, Depends(get_db)],
    rag=Depends(get_rag_client)) -> AnswerResponse:
    return await send_answer(session, user=user, inquiry_id=inquiry_id, payload=payload, key=key, rag=rag)


@router.post("/{inquiry_id}/close", response_model=CloseResponse,
    summary="최종 답변 이후 상담 종료·평가 요청",
    responses=documented_errors(*ERRORS, "INQUIRY_NOT_FOUND", "INQUIRY_CLOSED", "STALE_INQUIRY_REVISION", "ANSWER_REQUIRED", "IDEMPOTENCY_CONFLICT", bearer_auth=True),
    description="Idempotency-Key 필수. 답변이 있는 문의만 종료합니다. expected_inquiry_revision을 확인하고 종료와 평가 요청을 함께 저장합니다. 해결 상태는 미확인이며 종료만으로 해결 처리하지 않습니다.")
async def close(inquiry_id: InquiryId, payload: CloseRequest, key: IdempotencyKey,
    user: Annotated[AuthenticatedUser, Depends(get_user)], session: Annotated[AsyncSession, Depends(get_db)]) -> CloseResponse:
    return await close_inquiry(session, user=user, inquiry_id=inquiry_id, payload=payload, key=key)
