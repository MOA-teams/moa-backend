"""발송된 최종 답변과 최초 고객 평가. 원본 AI 초안은 별도로 보존한다."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Identity, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import public_id_default
from app.db.base import BaseEntity


class Answer(BaseEntity):
    __tablename__ = "answers"
    __table_args__ = (
        UniqueConstraint("inquiry_id", "inquiry_revision", name="uq_answers_inquiry_revision"),
        UniqueConstraint("draft_id", "based_on_message_id", name="uq_answers_draft_message"),
        CheckConstraint("agent_edit_ratio IS NULL OR (agent_edit_ratio >= 0 AND agent_edit_ratio <= 1)", name="ck_answer_edit_ratio"),
    )
    answer_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, default=public_id_default("answer"))
    inquiry_id: Mapped[int] = mapped_column(ForeignKey("inquiries.inquiry_id", ondelete="RESTRICT"), nullable=False)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.message_id", ondelete="RESTRICT"), nullable=False, unique=True)
    based_on_message_id: Mapped[int] = mapped_column(ForeignKey("messages.message_id", ondelete="RESTRICT"), nullable=False)
    inquiry_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    backend_knowledge_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("analysis_drafts.draft_id", ondelete="RESTRICT"))
    draft_revision: Mapped[int | None] = mapped_column(Integer)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("analysis_runs.run_id", ondelete="RESTRICT"))
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("users.user_id", ondelete="RESTRICT"))
    delivery_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    original_draft: Mapped[dict | None] = mapped_column(JSONB)
    original_verification: Mapped[dict | None] = mapped_column(JSONB)
    approval: Mapped[dict] = mapped_column(JSONB, nullable=False)
    agent_edit_ratio: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Feedback(BaseEntity):
    __tablename__ = "feedback"
    __table_args__ = (CheckConstraint("rating IS NULL OR (rating >= 1 AND rating <= 5)", name="ck_feedback_rating"),)
    inquiry_id: Mapped[int] = mapped_column(ForeignKey("inquiries.inquiry_id", ondelete="RESTRICT"), primary_key=True)
    answer_id: Mapped[int] = mapped_column(ForeignKey("answers.answer_id", ondelete="RESTRICT"), nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False)
    rating: Mapped[int | None] = mapped_column(Integer)
    resolved: Mapped[bool | None] = mapped_column(Boolean)
    comment: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
