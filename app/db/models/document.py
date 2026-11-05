"""원본 버전은 불변으로 보존하고, 검색 상태와 외부 전달 의도는 별도로 기록한다."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Identity, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import DocStatus, DocType, IndexingStatus
from app.db.base import BaseEntity


def enum_values(enum):
    return [item.value for item in enum]


class DocumentCounter(BaseEntity):
    __tablename__ = "document_counters"
    __table_args__ = (CheckConstraint("last_number BETWEEN 0 AND 999", name="ck_document_counter_limit"),)

    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), primary_key=True)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class Document(BaseEntity):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "ai_source_id", name="uq_documents_tenant_ai_id"),
        CheckConstraint("latest_version > 0", name="ck_document_latest_version"),
        Index("ix_documents_tenant_id", "tenant_id"),
    )

    doc_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), nullable=False)
    ai_source_id: Mapped[str] = mapped_column(String(7), nullable=False)
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False)


class DocumentVersion(BaseEntity):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("doc_id", "version", name="uq_document_versions_doc_version"),
        CheckConstraint("version > 0 AND source_revision > 0", name="ck_document_version_positive"),
        CheckConstraint("(original_text IS NULL) <> (attachment_id IS NULL)", name="ck_document_original_source"),
    )

    version_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("documents.doc_id", ondelete="RESTRICT"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    uploader_id: Mapped[int] = mapped_column(ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False)
    attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachments.attachment_id", ondelete="RESTRICT"), unique=True)
    original_text: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    doc_type: Mapped[DocType] = mapped_column(Enum(DocType, name="doc_type", values_callable=enum_values), nullable=False)
    authored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[DocStatus] = mapped_column(Enum(DocStatus, name="doc_status", values_callable=enum_values), nullable=False)
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    extracted_text: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # I03 응답 유실도 제출 가능성으로 취급해야 이후 검색 차단을 생략하지 않는다.
    submission_possible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    indexing_status: Mapped[IndexingStatus] = mapped_column(Enum(IndexingStatus, name="document_indexing_status", values_callable=enum_values), nullable=False, default=IndexingStatus.PENDING)
    searchable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    indexing_error: Mapped[str | None] = mapped_column(String(200))
    sync_error: Mapped[str | None] = mapped_column(String(200))


class DocumentJob(BaseEntity):
    __tablename__ = "document_jobs"
    __table_args__ = (
        UniqueConstraint("version_id", "source_revision", name="uq_document_jobs_version_revision"),
        Index("ix_document_jobs_due", "phase", "available_at"),
    )

    job_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.version_id", ondelete="RESTRICT"), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False)
    # event_id와 본문을 함께 보존하여 재시도 시 AI의 event fingerprint가 변하지 않는다.
    barrier: Mapped[dict | None] = mapped_column(JSONB)
    barrier_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    resubmissions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(32))
    poll_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(200))
