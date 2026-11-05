from app.schemas.analysis import AutomaticResponseView
from app.schemas.answers import AnswerAudit, FeedbackResponse, ResolutionStatus
from app.schemas.analysis import Analysis, SimilarInquiries, DraftView, Verification
from app.core.enums import RunStatus, DraftResult
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from app.core.enums import InquiryCategory, InquiryStatus, InquiryUrgency, MessageSender, MessageType
from app.schemas.attachments import AttachmentMetadata

AttachmentId = Annotated[str, Field(strict=True, pattern=r"^ATT-[0-9]{4}$")]


class InquiryEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    os: str | None = Field(default=None, max_length=200, description="고객 앱 OS 정보", examples=["iOS 18"])
    app_version: str | None = Field(default=None, max_length=100, description="고객 앱 버전", examples=["2.4.0"])
    error_code: str | None = Field(default=None, max_length=100, description="고객 앱이 수집한 오류 코드", examples=["E-502"])


class InquiryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=10000, description="첫 고객 메시지. 공백만 있는 본문은 거절합니다.", examples=["저장할 때 E-502가 떠요."])
    source_customer_id: str | None = Field(default=None, max_length=128, description="싱크노트 cid 등 외부 고객 식별 문자열. 문의 참고 정보이며 인증·소유권·기업 소속 판단에 사용하지 않습니다.", examples=["U-2001", None])
    customer_category: InquiryCategory = Field(default=InquiryCategory.OTHER, description="고객이 선택한 분류. AI 분류와 구분하며 계약의 한글 값만 받습니다.")
    env: InquiryEnvironment = Field(default_factory=InquiryEnvironment, description="고객 앱 환경 정보")
    attachments: list[AttachmentId] = Field(default_factory=list, max_length=10, description="먼저 업로드한 본인 첨부의 문자열 ID. 중복·다른 메시지에 연결된 첨부는 허용하지 않습니다.")

    @field_validator("message")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("내용이 필요합니다.")
        return value

    @field_validator("attachments")
    @classmethod
    def unique_attachments(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("첨부 ID는 중복될 수 없습니다.")
        return value


class MessageCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=10000, description="추가 고객 메시지. 공백은 보존하되 공백만 있는 본문은 거절합니다.", examples=["재실행해도 똑같아요."])
    attachments: list[AttachmentId] = Field(default_factory=list, max_length=10, description="본인이 업로드한 미연결 첨부의 문자열 ID")
    sender: Literal["customer"] = Field(default="customer", description="생략 가능. customer 이외의 발신자는 지정할 수 없습니다.")

    @field_validator("text")
    @classmethod
    def non_blank(cls, value: str) -> str:
        return InquiryCreateRequest.non_blank(value)

    @field_validator("attachments")
    @classmethod
    def unique_attachments(cls, value: list[int]) -> list[int]:
        return InquiryCreateRequest.unique_attachments(value)


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    message_id: str = Field(validation_alias=AliasChoices("public_id", "message_id"), description="외부 문자열 ID", examples=["MSG-000001"])
    answer_id: str | None = None
    sender: MessageSender = Field(description="실제 발신 구분. 고정 접수 안내도 ai_auto이며 LLM 생성이라는 뜻은 아닙니다.")
    message_type: MessageType = Field(description="메시지 용도")
    text: str = Field(description="고객에게 표시할 메시지 본문")
    attachments: list[AttachmentMetadata] = Field(default_factory=list, description="다운로드 직전에 첨부 조회 API로 URL을 발급받으세요.")
    sent_at: datetime = Field(description="서버 발송 시각, 타임존 포함 ISO 8601")


class InquiryCreateResponse(BaseModel):
    inquiry_id: str = Field(validation_alias=AliasChoices("public_id", "inquiry_id"), description="외부 문자열 ID", examples=["Q-1001"])
    status: InquiryStatus
    auto_message: MessageResponse = Field(description="문의·첫 메시지와 함께 저장된 고정 접수 안내")


class MessageCreateResponse(BaseModel):
    message_id: str = Field(validation_alias=AliasChoices("public_id", "message_id"), description="외부 문자열 ID", examples=["MSG-000001"])
    inquiry_id: str = Field(validation_alias=AliasChoices("public_id", "inquiry_id"), description="외부 문자열 ID", examples=["Q-1001"])
    inquiry_revision: int = Field(description="고객 메시지 추가 후의 문의 revision")
    status: InquiryStatus


class InquiryChatResponse(BaseModel):
    closed_at: datetime | None = None
    resolution_status: ResolutionStatus = "미확인"
    feedback_requested: bool = False
    feedback_submitted: bool = False
    feedback: FeedbackResponse | None = None
    inquiry_id: str = Field(validation_alias=AliasChoices("public_id", "inquiry_id"), description="외부 문자열 ID", examples=["Q-1001"])
    status: InquiryStatus
    agent_last_read_message_id: str | None = Field(default=None, description="담당자들이 읽은 가장 뒤의 메시지 ID. 읽음 기록이 없으면 null")
    messages: list[MessageResponse]
    next_cursor: str = Field(description="다음 페이지·다음 폴링에 그대로 사용. 빈 응답에도 마지막 위치를 유지합니다.")
    has_more: bool = Field(description="현재 응답 뒤에 즉시 읽을 메시지가 더 있는지 여부")


class InquirySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    inquiry_id: str = Field(validation_alias=AliasChoices("public_id", "inquiry_id"), description="외부 문자열 ID", examples=["Q-1001"])
    title: str
    customer_category: InquiryCategory
    category: InquiryCategory | None = Field(default=None, description="AI 분석 전 null")
    urgency: InquiryUrgency | None = Field(default=None, description="AI 분석 전 null")
    status: InquiryStatus
    created_at: datetime
    processing_stage: str | None = None
    group_id: str | None = Field(default=None, description="확정된 그룹 ID. 미분석·충돌 보류 시 null", pattern=r"^G-[0-9]{3}$")


class InquiryListResponse(BaseModel):
    items: list[InquirySummary]
    next_cursor: str | None
    has_more: bool


class CustomerPresence(BaseModel):
    status: Literal["상담 중", "나감"] = "상담 중"
    left_at: datetime | None = None
    left_reason: Literal["left", "new_chat"] | None = None


class InquiryDetailResponse(InquiryChatResponse):
    customer_presence: CustomerPresence
    customer_last_read_message_id: str | None = None
    automatic_response: AutomaticResponseView | None = None
    backend_knowledge_revision: int
    answers: list[AnswerAudit] = Field(default_factory=list)
    tenant_id: str
    customer_id: str
    source_customer_id: str | None = Field(default=None, description="외부에서 전달받은 참고용 고객 ID. 인증된 MOA customer_id와 별개이며 검증된 신원 정보가 아닙니다.")
    title: str
    revision: int
    customer_category: InquiryCategory
    env: InquiryEnvironment
    category: InquiryCategory | None
    urgency: InquiryUrgency | None
    created_at: datetime
    updated_at: datetime
    latest_customer_message_id: str
    processing_stage: str | None = None
    latest_run_id: str | None = None
    analysis_request_id: str | None = None
    run_status: RunStatus | None = None
    analysis_error: str | None = None
    analysis: Analysis | None = None
    additional_question: str | None = None
    similar_inquiries: SimilarInquiries | None = None
    knowledge_revision: int | None = None
    draft_result: DraftResult | None = None
    group_id: str | None = None
    draft: DraftView | None = None
    verification: Verification | None = None


class SessionEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["left", "new_chat", "returned"]


class SessionEventResponse(BaseModel):
    customer_presence: CustomerPresence


class ReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last_read_message_id: str = Field(strict=True, pattern=r"^MSG-[0-9]{6}$", description="화면에서 읽은 마지막 메시지 ID", examples=["MSG-000001"])


class ReadResponse(BaseModel):
    last_read_message_id: str = Field(description="실제 저장된 읽음 위치. 과거 ID로 요청해도 뒤로 이동하지 않습니다.")
