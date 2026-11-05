from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import InquiryCategory, InquiryStatus, InquiryUrgency, MessageSender, MessageType
from app.schemas.attachments import AttachmentMetadata

ResourceId = Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]


class InquiryEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    os: str | None = Field(default=None, max_length=200, description="고객 앱 OS 정보", examples=["iOS 18"])
    app_version: str | None = Field(default=None, max_length=100, description="고객 앱 버전", examples=["2.4.0"])
    error_code: str | None = Field(default=None, max_length=100, description="고객 앱이 수집한 오류 코드", examples=["E-502"])


class InquiryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=10000, description="첫 고객 메시지. 공백만 있는 본문은 거절합니다.", examples=["저장할 때 E-502가 떠요."])
    source_customer_id: str | None = Field(default=None, max_length=128, description="싱크노트 cid 등 외부 고객 식별 문자열. 문의 참고 정보이며 인증·소유권·기업 소속 판단에 사용하지 않습니다.", examples=["U-2001", None])
    customer_category: InquiryCategory = Field(default=InquiryCategory.OTHER, description="고객이 선택한 분류. AI 분류와 구분하며 영어 값만 받습니다.")
    env: InquiryEnvironment = Field(default_factory=InquiryEnvironment, description="고객 앱 환경 정보")
    attachments: list[ResourceId] = Field(default_factory=list, max_length=10, description="먼저 업로드한 본인 첨부의 숫자 ID. 중복·다른 메시지에 연결된 첨부는 허용하지 않습니다.")

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
    attachments: list[ResourceId] = Field(default_factory=list, max_length=10, description="본인이 업로드한 미연결 첨부의 숫자 ID")
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

    message_id: int = Field(description="숫자 메시지 ID. 프론트에서는 이 값으로 중복 제거합니다.")
    sender: MessageSender = Field(description="실제 발신 구분. 고정 접수 안내도 ai_auto이며 LLM 생성이라는 뜻은 아닙니다.")
    message_type: MessageType = Field(description="메시지 용도")
    text: str = Field(description="고객에게 표시할 메시지 본문")
    attachments: list[AttachmentMetadata] = Field(default_factory=list, description="다운로드 직전에 첨부 조회 API로 URL을 발급받으세요.")
    sent_at: datetime = Field(description="서버 발송 시각, 타임존 포함 ISO 8601")


class InquiryCreateResponse(BaseModel):
    inquiry_id: int = Field(description="숫자 문의 ID")
    status: InquiryStatus
    auto_message: MessageResponse = Field(description="문의·첫 메시지와 함께 저장된 고정 접수 안내")


class MessageCreateResponse(BaseModel):
    message_id: int
    inquiry_id: int
    inquiry_revision: int = Field(description="고객 메시지 추가 후의 문의 revision")
    status: InquiryStatus


class InquiryChatResponse(BaseModel):
    inquiry_id: int
    status: InquiryStatus
    messages: list[MessageResponse]
    next_cursor: str = Field(description="다음 페이지·다음 폴링에 그대로 사용. 빈 응답에도 마지막 위치를 유지합니다.")
    has_more: bool = Field(description="현재 응답 뒤에 즉시 읽을 메시지가 더 있는지 여부")


class InquirySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    inquiry_id: int
    title: str
    customer_category: InquiryCategory
    category: InquiryCategory | None = Field(default=None, description="AI 분석 전 null")
    urgency: InquiryUrgency | None = Field(default=None, description="AI 분석 전 null")
    status: InquiryStatus
    created_at: datetime
    processing_stage: None = Field(default=None, description="5단계 AI 결과 연동 전 null")
    group_id: None = Field(default=None, description="9단계 그룹 연동 전 null")


class InquiryListResponse(BaseModel):
    items: list[InquirySummary]
    next_cursor: str | None
    has_more: bool


class InquiryDetailResponse(InquiryChatResponse):
    tenant_id: str
    customer_id: int
    source_customer_id: str | None = Field(default=None, description="외부에서 전달받은 참고용 고객 ID. 인증된 MOA customer_id와 별개이며 검증된 신원 정보가 아닙니다.")
    title: str
    revision: int
    customer_category: InquiryCategory
    env: InquiryEnvironment
    category: InquiryCategory | None
    urgency: InquiryUrgency | None
    created_at: datetime
    updated_at: datetime
    latest_customer_message_id: int
    processing_stage: None = None
    latest_run_id: None = None
    group_id: None = None
    draft: None = None
    verification: None = None
