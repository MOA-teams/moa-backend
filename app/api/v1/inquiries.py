from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.documentation import documented_errors
from app.core.enums import UserRole
from app.deps.auth import get_user, require_roles
from app.deps.db import get_db
from app.schemas.auth import AuthenticatedUser
from app.schemas.inquiries import InquiryChatResponse, InquiryCreateRequest, InquiryCreateResponse, MessageCreateRequest, MessageCreateResponse
from app.services.inquiries import add_message, create_inquiry, get_customer_chat

router = APIRouter(prefix="/inquiries", tags=["inquiries"], dependencies=[Depends(require_roles(UserRole.CUSTOMER))])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200, pattern=r"^[!-~]+$", description="논리 요청마다 생성한 UUID 등 ASCII 키. 재시도에는 같은 키·본문을 사용하고 새 메시지에는 새 키를 사용하세요.")]
InquiryId = Annotated[str, Path(pattern=r"^Q-[0-9]{4}$", description="외부 문의 ID")]
Cursor = Annotated[str | None, Query(max_length=2048, description="서버의 next_cursor를 그대로 전달. 최초 조회는 생략합니다.")]
Limit = Annotated[int, Query(ge=1, le=100, description="페이지 크기. 기본 20, 최대 100")]
COMMON_ERRORS = ("VALIDATION_ERROR", "INVALID_ACCESS_TOKEN", "FORBIDDEN", "TENANT_REQUIRED")


@router.post(
    "", status_code=201, response_model=InquiryCreateResponse,
    summary="문의 접수와 첫 메시지·접수 안내 저장",
    description="""기업 소속 customer 전용. tenant_id·customer_id·created_at은 보내지 않습니다.
문의·첫 고객 메시지·고정 접수 안내·첨부 연결을 함께 저장하며 분석 요청을 함께 보존하며 AI 완료를 기다리지 않습니다.
customer_category는 한글 Enum이고 실제 AI 분류는 아직 null입니다.
싱크노트 진입 query의 cid는 선택 필드 source_customer_id로 보냅니다(문자열, 최대 128자).
source_customer_id는 참고 정보로만 저장하며 실제 고객·기업은 로그인한 사용자로 결정합니다.
os·app_version·error_code는 env에 보내세요. 로그인·회원가입 전후 query 보존은 프론트에서 처리합니다.
첨부는 먼저 /attachments로 업로드한 문자열 ID 배열을 보냅니다(최대 10개).
Idempotency-Key는 필수이며 같은 키·본문의 재시도는 같은 201 응답입니다. 같은 키·다른 본문은 409입니다.
문의 ID는 Q-1001 형식의 문자열입니다.""",
    responses=documented_errors(*COMMON_ERRORS, "IDEMPOTENCY_CONFLICT", "ATTACHMENT_NOT_FOUND", "ATTACHMENT_ALREADY_LINKED", bearer_auth=True),
)
async def create(
    payload: Annotated[InquiryCreateRequest, Body(openapi_examples={
        "plain": {"summary": "첨부 없는 문의", "value": {"message": "저장할 때 E-502가 떠요.", "customer_category": "기능 오류", "env": {"os": "iOS 18", "app_version": "2.4.0", "error_code": "E-502"}, "attachments": []}},
        "syncnote": {"summary": "로그인 후 싱크노트에서 전달된 고객·오류 정보로 접수", "value": {"message": "저장할 때 오류가 발생해요.", "source_customer_id": "U-2001", "customer_category": "기능 오류", "env": {"os": "iOS 18", "app_version": "2.4.0", "error_code": "E-502"}, "attachments": []}},
        "attached": {"summary": "본인이 업로드한 첨부 ID로 교체", "value": {"message": "오류 화면입니다.", "customer_category": "기능 오류", "attachments": ["ATT-0001"]}},
    })],
    key: IdempotencyKey,
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> InquiryCreateResponse:
    return await create_inquiry(session, user=user, payload=payload, key=key)


@router.post(
    "/{inquiry_id}/messages", status_code=201, response_model=MessageCreateResponse,
    summary="본인 문의에 고객 메시지 추가",
    description="""본인·같은 기업의 문의에만 추가합니다. 다른 고객 또는 기업의 문의는 404입니다.
sender는 생략하거나 customer만 보낼 수 있습니다. agent·ai_auto 지정은 허용하지 않습니다.
Idempotency-Key 필수. 성공 시 문의 revision을 증가시키고 상태를 접수로 설정합니다.
상담 종료 상태는 409이므로 새 문의를 생성하세요. 새 revision으로 분석 요청을 보존합니다.""",
    responses=documented_errors(*COMMON_ERRORS, "IDEMPOTENCY_CONFLICT", "INQUIRY_NOT_FOUND", "INQUIRY_CLOSED", "ATTACHMENT_NOT_FOUND", "ATTACHMENT_ALREADY_LINKED", bearer_auth=True),
)
async def append(
    inquiry_id: InquiryId,
    payload: Annotated[MessageCreateRequest, Body(openapi_examples={"message": {"summary": "추가 고객 메시지", "value": {"text": "재실행해도 똑같아요.", "attachments": []}}})],
    key: IdempotencyKey,
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> MessageCreateResponse:
    return await add_message(session, user=user, inquiry_id=inquiry_id, payload=payload, key=key)


@router.get(
    "/{inquiry_id}", response_model=InquiryChatResponse,
    summary="본인 문의의 상태와 채팅 이력 조회",
    description="""최초에는 cursor 없이, 다음부터 next_cursor로 조회합니다. 문의별 저장 순서로 오래된 메시지부터 반환합니다.
has_more=true이면 다음 페이지를 바로 읽고, false이면 3초 뒤 같은 next_cursor로 폴링하세요.
빈 응답에도 마지막 cursor 위치가 유지되며 message_id로 프론트에서 중복 제거합니다.
첨부에는 메타데이터만 포함합니다. 다운로드 직전에 /attachments/{id}로 URL을 발급받으세요.
고객에게 내부 초안·분류·검수 정보를 반환하지 않습니다. 변조되거나 다른 문의·사용자의 cursor는 400입니다.""",
    responses=documented_errors(*COMMON_ERRORS, "INQUIRY_NOT_FOUND", "INVALID_CURSOR", bearer_auth=True),
)
async def chat(
    inquiry_id: InquiryId,
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    cursor: Cursor = None, limit: Limit = 20,
) -> InquiryChatResponse:
    return await get_customer_chat(session, user=user, inquiry_id=inquiry_id, cursor=cursor, limit=limit)


from app.schemas.answers import FeedbackRequest, FeedbackResponse
from app.services.answers import submit_feedback


@router.post("/{inquiry_id}/feedback", status_code=201, response_model=FeedbackResponse,
    summary="종료된 본인 상담의 최초 평가",
    responses=documented_errors(*COMMON_ERRORS, "INQUIRY_NOT_FOUND", "INQUIRY_NOT_CLOSED", "FEEDBACK_ALREADY_SUBMITTED", "ANSWER_REQUIRED", "IDEMPOTENCY_CONFLICT", bearer_auth=True),
    description="Idempotency-Key 필수. rating(정수 1~5)·resolved(boolean)·comment 중 하나 이상 제출합니다. 별점은 해결 여부를 대신하지 않습니다. 평가는 종료 당시 마지막 답변에 한 번만 연결하며, 동일 키·본문 재시도만 기존 결과를 반환합니다. 미해결 평가로 상담을 재개하지 않습니다.")
async def feedback(inquiry_id: InquiryId, payload: FeedbackRequest, key: IdempotencyKey,
    user: Annotated[AuthenticatedUser, Depends(get_user)], session: Annotated[AsyncSession, Depends(get_db)]) -> FeedbackResponse:
    return await submit_feedback(session, user=user, inquiry_id=inquiry_id, payload=payload, key=key)
