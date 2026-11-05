from app.core.ids import public_id_default
from datetime import datetime

from sqlalchemy import Boolean, BigInteger, CheckConstraint, DateTime, Enum, ForeignKey, Identity, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import InquiryCategory, InquiryStatus, InquiryUrgency, MessageSender, MessageType
from app.db.base import BaseEntity

category_type = Enum(InquiryCategory, name="inquiry_category", values_callable=lambda values: [v.value for v in values])


class Inquiry(BaseEntity):
    public_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, default=public_id_default("inquiry"))
    __tablename__ = "inquiries"
    __table_args__ = (
        Index("ix_inquiries_tenant_created_id", "tenant_id", "created_at", "inquiry_id"),
        Index("ix_inquiries_customer_id", "customer_id"),
        Index("ix_inquiries_group_pk", "group_pk"),
        CheckConstraint("revision > 0 AND last_message_sequence >= 0", name="ck_inquiries_revision_sequence"),
    )

    inquiry_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False)
    group_pk: Mapped[int | None] = mapped_column(ForeignKey("inquiry_groups.group_id", ondelete="RESTRICT"))
    normalized_error_code: Mapped[str | None] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    is_seed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    source_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[int | None] = mapped_column(ForeignKey("users.user_id", ondelete="RESTRICT"))
    feedback_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="미확인")
    latest_analysis_request_id: Mapped[str | None] = mapped_column(String(36))
    classification_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    processing_stage: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_category: Mapped[InquiryCategory] = mapped_column(category_type, nullable=False)
    env: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[InquiryStatus] = mapped_column(
        Enum(InquiryStatus, name="inquiry_status", values_callable=lambda values: [v.value for v in values]),
        nullable=False,
    )
    category: Mapped[InquiryCategory | None] = mapped_column(category_type)
    urgency: Mapped[InquiryUrgency | None] = mapped_column(
        Enum(InquiryUrgency, name="inquiry_urgency", values_callable=lambda values: [v.value for v in values]),
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_message_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class Message(BaseEntity):
    public_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, default=public_id_default("message"))
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("inquiry_id", "sequence", name="uq_messages_inquiry_sequence"),
        CheckConstraint("sequence > 0", name="ck_messages_sequence"),
    )

    message_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    inquiry_id: Mapped[int] = mapped_column(ForeignKey("inquiries.inquiry_id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender: Mapped[MessageSender] = mapped_column(
        Enum(MessageSender, name="message_sender", values_callable=lambda values: [v.value for v in values]), nullable=False,
    )
    message_type: Mapped[MessageType] = mapped_column(
        Enum(MessageType, name="message_type", values_callable=lambda values: [v.value for v in values]), nullable=False,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
