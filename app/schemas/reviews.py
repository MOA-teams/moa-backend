from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator
from app.schemas.analysis import Contract

ReviewID = Annotated[str, Field(pattern=r"^REV-[a-f0-9]{32}$")]
ReviewStatus = Literal["pending", "resolved", "dismissed"]


class ReviewDecision(Contract):
    action: Literal["approve", "edit", "dismiss"]
    expected_revision: int = Field(strict=True, ge=1)
    text: str | None = Field(default=None, min_length=1, max_length=200000)

    @model_validator(mode="after")
    def content(self):
        if self.action == "edit":
            if self.text is None or not self.text.strip():
                raise ValueError("직접 수정할 본문이 필요합니다.")
        elif self.text is not None:
            raise ValueError("text는 edit에서만 사용합니다.")
        return self


class BulkItem(Contract):
    review_id: ReviewID
    expected_revision: int = Field(strict=True, ge=1)


class BulkApproval(Contract):
    items: list[BulkItem] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique(self):
        if len({item.review_id for item in self.items}) != len(self.items):
            raise ValueError("중복 재검토 ID는 지정할 수 없습니다.")
        return self


class DecisionResult(Contract):
    review_id: str
    revision: int
    status: ReviewStatus
    resolved_at: datetime
    result: dict | None = None


class BulkResultItem(Contract):
    review_id: str
    success: bool
    status_code: int
    error_code: str | None = None
    result: DecisionResult | None = None


class BulkResult(Contract):
    items: list[BulkResultItem]


class ReviewView(Contract):
    review_id: str
    target_type: Literal["draft", "document"]
    target_id: str
    target_version: int
    inquiry_id: str | None
    revision: int
    title: str
    reason: str
    source: Literal["push", "doc_version", "existing_state"]
    push_event_id: str | None
    current_text: str
    suggested_text: str | None
    suggestion_status: str
    suggestion_error: str | None
    excluded_from_search: bool | None
    status: ReviewStatus
    created_at: datetime
    resolved_at: datetime | None
    result: dict | None


class ReviewPage(Contract):
    items: list[ReviewView]
    next_cursor: str | None
    has_more: bool


class CommitView(Contract):
    sha: str
    message: str
    author: str
    committed_at: datetime
    files: list[str] = Field(default_factory=list)


class CommitPage(Contract):
    items: list[CommitView]
    next_cursor: str | None
    has_more: bool


class ChangeView(Contract):
    path: str
    previous_path: str | None
    summary: str
    excluded: bool
    exclude_reason: str | None
    diff: str | None
    diff_complete: bool


class PushEventView(Contract):
    push_event_id: str
    repo: str
    branch: str
    before_sha: str | None
    after_sha: str | None
    commit: CommitView | None
    summary: str | None
    summary_kind: Literal["change_statistics"]
    changes: list[ChangeView]
    affected_draft_ids: list[str]
    affected_document_ids: list[str]
    status: Literal["pending", "processing", "completed", "incomplete", "failed"]
    complete: bool
    error_code: str | None
    created_at: datetime


class PushEventPage(Contract):
    items: list[PushEventView]
    next_cursor: str | None
    has_more: bool
