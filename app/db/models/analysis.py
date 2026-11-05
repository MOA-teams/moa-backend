"""요청 전달과 결과 적용 이력. AI 내부 단계 실행은 AI 서비스의 책임이다."""

from datetime import datetime
from uuid import uuid4
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.enums import RunStatus
from app.core.ids import public_id_default
from app.db.base import BaseEntity


class AnalysisRequest(BaseEntity):
    __tablename__ = "analysis_requests"
    __table_args__ = (
        Index("ix_analysis_due", "phase", "available_at"),
        Index("ix_analysis_inquiry", "inquiry_id"),
    )
    request_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    inquiry_id: Mapped[int] = mapped_column(
        ForeignKey("inquiries.inquiry_id", ondelete="CASCADE"), nullable=False
    )
    backend_knowledge_revision: Mapped[int | None] = mapped_column(BigInteger)
    inquiry_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    based_on_message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.message_id", ondelete="CASCADE"), nullable=False
    )
    knowledge_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    review_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    run_status: Mapped[RunStatus] = mapped_column(
        Enum(
            RunStatus,
            name="pipeline_run_status",
            values_callable=lambda e: [v.value for v in e],
        ),
        nullable=False,
        default=RunStatus.PENDING,
    )
    phase: Mapped[str] = mapped_column(String(20), nullable=False, default="submit")
    run_id: Mapped[str | None] = mapped_column(String(100))
    current_stage: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(100))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resubmissions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(32))


class AnalysisRun(BaseEntity):
    __tablename__ = "analysis_runs"
    __table_args__ = (Index("ix_analysis_runs_request_id", "request_id"),)
    run_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_requests.request_id", ondelete="CASCADE"), nullable=False
    )
    superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result_hash: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict | None] = mapped_column(JSONB)
    response: Mapped[dict | None] = mapped_column(JSONB)


class AnalysisDraft(BaseEntity):
    __tablename__ = "analysis_drafts"
    draft_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, default=public_id_default("draft")
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_runs.run_id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    review_reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


class ClassificationHistory(BaseEntity):
    __tablename__ = "classification_history"
    __table_args__ = (Index("ix_classification_history_inquiry_id", "inquiry_id"),)
    history_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    inquiry_id: Mapped[int] = mapped_column(
        ForeignKey("inquiries.inquiry_id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[int] = mapped_column(
        ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    before: Mapped[dict] = mapped_column(JSONB, nullable=False)
    after: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
