from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.enums import DocStatus, DocType, IndexingStatus

DocumentId = Annotated[str, Field(strict=True, pattern=r"^DOC-[0-9]{3}$")]
AttachmentId = Annotated[str, Field(strict=True, pattern=r"^ATT-[0-9]{4}$")]


class DocumentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: DocumentId | None = None
    version: int = Field(strict=True, ge=1, le=2147483647)
    doc_type: DocType
    title: str = Field(min_length=1, max_length=300)
    status: DocStatus = DocStatus.VALID
    created_at: datetime = Field(description="원본 작성 시각. 타임존 필수")
    text: str | None = Field(default=None, min_length=1, max_length=200_000)
    attachment_id: AttachmentId | None = None

    @field_validator("created_at")
    @classmethod
    def timezone_required(cls, value):
        if value.utcoffset() is None:
            raise ValueError("타임존이 필요합니다.")
        return value

    @field_validator("title", "text")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("내용이 필요합니다.")
        return value

    @model_validator(mode="after")
    def source_and_version(self) -> Self:
        if (self.text is None) == (self.attachment_id is None):
            raise ValueError("text와 attachment_id 중 하나만 지정하세요.")
        if self.doc_id is None and self.version != 1:
            raise ValueError("신규 문서는 버전 1이어야 합니다.")
        return self


class DocumentAccepted(BaseModel):
    doc_id: str
    version: int
    status: DocStatus
    indexing_status: IndexingStatus
    uploaded_at: datetime


class DocumentStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: DocStatus
    review_required: bool = Field(strict=True)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value):
        return DocumentCreateRequest.nonblank(value)


class DocumentStatusResponse(BaseModel):
    doc_id: str
    version: int
    status: DocStatus
    review_required: bool
    searchable: bool


class DocumentSummary(DocumentStatusResponse):
    title: str
    doc_type: DocType
    created_at: datetime
    uploaded_at: datetime
    attachment_id: str | None
    indexing_status: IndexingStatus
    indexing_error: str | None
    sync_error: str | None


class DocumentListResponse(BaseModel):
    items: list[DocumentSummary]
    next_cursor: str | None
    has_more: bool
