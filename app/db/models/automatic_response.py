"""자동 발송과 검토 전환의 영속 이력. 고객 메시지와 같은 트랜잭션에 기록한다."""

from sqlalchemy import ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import BaseEntity


class AutomaticResponse(BaseEntity):
    __tablename__ = "automatic_responses"
    __table_args__ = (
        Index("ix_automatic_responses_inquiry", "inquiry_id"),
        Index(
            "uq_automatic_response_customer_message",
            "inquiry_id",
            "based_on_message_id",
            unique=True,
            postgresql_where=text("message_id IS NOT NULL"),
        ),
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_runs.run_id", ondelete="RESTRICT"), primary_key=True
    )
    inquiry_id: Mapped[int] = mapped_column(
        ForeignKey("inquiries.inquiry_id", ondelete="RESTRICT"), nullable=False
    )
    based_on_message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.message_id", ondelete="RESTRICT"), nullable=False
    )
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.message_id", ondelete="RESTRICT"), unique=True
    )
    draft_id: Mapped[int | None] = mapped_column(
        ForeignKey("analysis_drafts.draft_id", ondelete="RESTRICT")
    )
    delivered_revision: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    review_reasons: Mapped[list] = mapped_column(JSONB, nullable=False)
    policy: Mapped[dict] = mapped_column(JSONB, nullable=False)
    validation: Mapped[dict] = mapped_column(JSONB, nullable=False)
