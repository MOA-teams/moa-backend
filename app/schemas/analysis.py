from typing import Annotated, Literal, Self
from pathlib import PurePosixPath
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    AwareDatetime,
    field_validator,
    model_validator,
)
from app.core.enums import (
    InquiryCategory,
    InquiryUrgency,
    RunStatus,
    DraftResult,
    CheckStatus,
    MessageSender,
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


InquiryID = Annotated[str, Field(pattern=r"^Q-[0-9]{4}$")]
MessageID = Annotated[str, Field(pattern=r"^MSG-[0-9]{6}$")]
RunID = Annotated[
    str, Field(min_length=1, max_length=100, pattern=r"^RUN-[A-Za-z0-9-]+$")
]


class SnapshotMessage(Contract):
    answer_id: str | None = None
    message_id: MessageID
    sender: MessageSender
    text: str = Field(min_length=1, max_length=50000)
    sent_at: AwareDatetime
    attachments: list[str] = Field(default_factory=list)


class AnalysisAccepted(Contract):
    inquiry_id: InquiryID
    analysis_request_id: str
    run_id: RunID | None = None
    run_status: RunStatus


class AIMessageAccepted(Contract):
    run_id: RunID
    inquiry_id: InquiryID
    inquiry_revision: int = Field(ge=1)
    run_status: RunStatus


class AnalysisRetry(Contract):
    expected_inquiry_revision: int = Field(strict=True, ge=1)
    based_on_message_id: MessageID
    review_only: bool = Field(default=True, strict=True)


class ClassificationRequest(Contract):
    expected_revision: int = Field(strict=True, ge=1)
    category: InquiryCategory
    urgency: InquiryUrgency
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("사유가 필요합니다.")
        return value


class ClassificationResponse(Contract):
    inquiry_id: InquiryID
    revision: int
    category: InquiryCategory
    urgency: InquiryUrgency


class DocEvidence(Contract):
    doc_id: str = Field(pattern=r"^DOC-[0-9]{3}$")
    version: int = Field(ge=1)


class CodeEvidence(Contract):
    path: str = Field(min_length=1, max_length=1000)
    commit_sha: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{7,40}$")

    @field_validator("path")
    @classmethod
    def relative_path(cls, value):
        p = PurePosixPath(value)
        if p.is_absolute() or ".." in p.parts or "\\" in value:
            raise ValueError("상대 경로가 필요합니다.")
        return value


class Evidence(Contract):
    docs: list[DocEvidence] = Field(default_factory=list)
    code: list[CodeEvidence] = Field(default_factory=list)


class Analysis(Contract):
    category: InquiryCategory
    urgency: InquiryUrgency
    summary: str
    normalized_error_code: str | None = None
    missing_fields: list[str] = Field(default_factory=list)


class SimilarInquiry(Contract):
    inquiry_id: InquiryID
    group_id: str | None = None
    score: float = Field(ge=0, le=1)


class SimilarInquiries(Contract):
    items: list[SimilarInquiry] = Field(default_factory=list)
    suggested_group_id: str | None = None
    reason: str | None = None


class Citation(Contract):
    text: str
    docs: list[DocEvidence] = Field(default_factory=list)


class StyleReference(Contract):
    best_id: str = Field(pattern=r"^BA-[0-9]{3}$")
    version: int = Field(ge=1)


class Draft(Contract):
    answer_text: str
    evidence: Evidence
    citations: list[Citation] = Field(default_factory=list)
    style_references: list[StyleReference] = Field(default_factory=list)


class Verification(Contract):
    status: CheckStatus
    checks: dict[str, CheckStatus] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    unsupported_sentences: list[str] = Field(default_factory=list)


class PipelineError(Contract):
    code: str = Field(min_length=1, max_length=100)
    message: str
    retryable: bool = False


class TeamRouting(Contract):
    team: Literal["CS", "테크", "판단 보류"] = "판단 보류"
    reason: str = Field(
        default="담당 팀을 판단할 근거가 부족합니다.", min_length=1, max_length=500
    )
    requires_human: bool = Field(default=True, strict=True)


class PipelineResult(Contract):
    run_id: RunID
    tenant_id: str = Field(pattern=r"^[a-z]{1,64}$")
    inquiry_id: InquiryID
    inquiry_revision: int = Field(ge=1)
    based_on_message_id: MessageID
    run_status: RunStatus
    current_stage: str | None = Field(default=None, max_length=100)
    knowledge_revision: int | None = Field(default=None, ge=0)
    analysis: Analysis | None = None
    similar_inquiries: SimilarInquiries | None = None
    draft_result: DraftResult | None = None
    additional_question: str | None = None
    draft: Draft | None = None
    verification: Verification | None = None
    error: PipelineError | None = None
    # AI의 기존 callback과 같은 표현으로 정규화한다. 선택 필드 추가만으로
    # 이전 결과의 지문이 달라지거나 poll/callback이 서로 충돌하지 않게 한다.
    routing: TeamRouting | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def routing_review_note(self) -> Self:
        if self.routing is not None and self.verification is not None:
            note = f"담당 팀: {self.routing.team}. {self.routing.reason}"
            if note not in self.verification.reasons:
                self.verification.reasons.append(note)
        return self

    @model_validator(mode="after")
    def terminal_shape(self) -> Self:
        if self.run_status == RunStatus.COMPLETED and (
            self.analysis is None or self.draft_result is None or self.error is not None
        ):
            raise ValueError("완료 결과가 불완전합니다.")
        if self.run_status == RunStatus.FAILED:
            if self.error is None or any(
                v is not None
                for v in (
                    self.analysis,
                    self.draft,
                    self.draft_result,
                    self.verification,
                    self.additional_question,
                )
            ):
                raise ValueError("실패 결과는 오류만 포함해야 합니다.")
        if (
            self.run_status == RunStatus.COMPLETED
            and self.draft_result == DraftResult.READY
            and (self.draft is None or self.verification is None)
        ):
            raise ValueError("초안과 검수 결과가 필요합니다.")
        if (
            self.run_status == RunStatus.COMPLETED
            and self.draft_result == DraftResult.NEED_INFO
            and not self.additional_question
        ):
            raise ValueError("추가 질문이 필요합니다.")
        return self


class ApplyResponse(Contract):
    run_id: RunID
    apply_status: Literal["applied", "duplicate", "ignored_stale"]
    inquiry_id: InquiryID
    status: str
    draft_id: str | None = None
    group_id: str | None = Field(default=None, pattern=r"^G-[0-9]{3}$")
    delivery_action: Literal["담당자 검토", "없음", "추가 질문 전송", "자동 답변 전송"]


class DraftView(Draft):
    draft_id: str
    revision: int
    draft_result: DraftResult
    review_reasons: list[str] = Field(default_factory=list)


class InformationCollectionView(Contract):
    status: Literal[
        "질문 필요", "정보 충분", "고객 응답 대기", "자동 질문 꺼짐", "질문 한도 도달",
        "추가 답변 후에도 정보 부족", "담당자 확인 필요", "검증 또는 발송 확인 필요",
    ]
    stage: Literal["initial", "followup", "exhausted", "sufficient"]
    questions_sent: int = Field(ge=0)
    max_questions: int = Field(ge=1, le=3)
    missing_fields: list[str]
    handoff_required: bool
    handoff_reason: str | None


class AutomaticResponseView(Contract):
    answer_id: str | None = None
    run_id: RunID
    delivery_action: Literal["담당자 검토", "추가 질문 전송", "자동 답변 전송"]
    message_id: MessageID | None
    review_reasons: list[str]
    requires_review: bool
    delivery_mode: Literal["자동"] | None
    agent_id: None = None
    information_collection: InformationCollectionView | None = None
