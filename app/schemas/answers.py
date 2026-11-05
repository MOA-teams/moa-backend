from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator
from app.core.enums import InquiryStatus
from app.schemas.analysis import Contract, Evidence, MessageID

ResolutionStatus = Literal["미확인", "해결 확인", "미해결"]


class AnswerRequest(Contract):
    draft_id: str | None = Field(default=None, pattern=r"^DRAFT-[0-9]{6}$")
    draft_revision: int | None = Field(default=None, strict=True, ge=1)
    expected_inquiry_revision: int = Field(strict=True, ge=1)
    expected_last_customer_message_id: MessageID
    expected_backend_knowledge_revision: int = Field(strict=True, ge=0)
    answer_text: str = Field(min_length=1, max_length=10000)
    evidence: Evidence = Field(default_factory=Evidence)

    @model_validator(mode="after")
    def validate_answer(self):
        if (self.draft_id is None) != (self.draft_revision is None):
            raise ValueError("초안 ID와 revision은 함께 지정해야 합니다.")
        if not self.answer_text.strip():
            raise ValueError("답변 내용이 필요합니다.")
        return self


class AnswerResponse(Contract):
    answer_id: str
    message_id: str
    inquiry_id: str
    inquiry_revision: int
    status: InquiryStatus
    delivery_mode: Literal["담당자"]
    agent_id: str
    answered_at: datetime


class CloseRequest(Contract):
    expected_inquiry_revision: int = Field(strict=True, ge=1)
    resolution_status: Literal["미확인"] = "미확인"


class CloseResponse(Contract):
    inquiry_id: str
    inquiry_revision: int
    status: InquiryStatus
    resolution_status: ResolutionStatus
    closed_at: datetime
    feedback_requested: bool


class FeedbackRequest(Contract):
    rating: int | None = Field(default=None, strict=True, ge=1, le=5)
    resolved: bool | None = Field(default=None, strict=True)
    comment: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def meaningful(self):
        if self.comment is not None and not self.comment.strip():
            raise ValueError("댓글은 공백만 지정할 수 없습니다.")
        if self.rating is None and self.resolved is None and self.comment is None:
            raise ValueError("별점, 해결 여부 또는 댓글이 필요합니다.")
        return self


class FeedbackResponse(FeedbackRequest):
    inquiry_id: str
    answer_id: str
    resolution_status: ResolutionStatus
    recorded_at: datetime


class AnswerAudit(Contract):
    answer_id: str
    message_id: str
    answer_text: str
    evidence: Evidence
    original_draft: dict | None
    original_verification: dict | None
    approval: dict
    agent_edit_ratio: float | None
    delivery_mode: str
    agent_id: str | None
    run_id: str | None
    draft_revision: int | None
    answered_at: datetime
