"""변경 원인과 담당자가 실제 확인한 수정안의 revision을 보존한다."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseEntity


class PushEvent(BaseEntity):
    __tablename__ = "push_events"
    __table_args__ = (Index("ix_push_events_due", "status", "available_at"), Index("ix_push_events_tenant", "tenant_id", "event_id"))

    event_id: Mapped[int] = mapped_column(ForeignKey("github_events.event_id", ondelete="RESTRICT"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), nullable=False)
    integration_id: Mapped[str] = mapped_column(ForeignKey("github_integrations.integration_id", ondelete="RESTRICT"), nullable=False)
    job_id: Mapped[int] = mapped_column(ForeignKey("github_jobs.job_id", ondelete="RESTRICT"), nullable=False)
    repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repository: Mapped[str] = mapped_column(String(200), nullable=False)
    branch: Mapped[str] = mapped_column(String(200), nullable=False)
    before_sha: Mapped[str | None] = mapped_column(String(40))
    after_sha: Mapped[str | None] = mapped_column(String(40))
    security_level: Mapped[int] = mapped_column(Integer, nullable=False)
    include_paths: Mapped[list] = mapped_column(JSONB, nullable=False)
    exclude_patterns: Mapped[list] = mapped_column(JSONB, nullable=False)
    document_bindings: Mapped[list] = mapped_column(JSONB, nullable=False)
    commit_info: Mapped[dict | None] = mapped_column(JSONB)
    changes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    summary: Mapped[str | None] = mapped_column(Text)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error_code: Mapped[str | None] = mapped_column(String(100))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(32))

    @property
    def public_id(self):
        return f"EVENT-{self.event_id:06d}"


class ReviewItem(BaseEntity):
    __tablename__ = "review_items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "target_type", "target_id", "target_version", name="uq_review_target_version"),
        Index("ix_review_items_tenant_status", "tenant_id", "status", "review_id"),
    )

    review_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, default=lambda: "REV-" + uuid4().hex)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), nullable=False)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(40), nullable=False)
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    inquiry_id: Mapped[int | None] = mapped_column(ForeignKey("inquiries.inquiry_id", ondelete="RESTRICT"))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.doc_id", ondelete="RESTRICT"))
    push_event_id: Mapped[int | None] = mapped_column(ForeignKey("push_events.event_id", ondelete="RESTRICT"))
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    cause_keys: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    current_text: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    suggestion_status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    suggestion_error: Mapped[str | None] = mapped_column(String(100))
    analysis_request_id: Mapped[str | None] = mapped_column(ForeignKey("analysis_requests.request_id", ondelete="RESTRICT"))
    suggested_draft_id: Mapped[int | None] = mapped_column(ForeignKey("analysis_drafts.draft_id", ondelete="RESTRICT"))
    suggested_text: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str | None] = mapped_column(String(20))
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.user_id", ondelete="RESTRICT"))
    result: Mapped[dict | None] = mapped_column(JSONB)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
