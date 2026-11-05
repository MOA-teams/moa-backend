from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Identity, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseEntity


class Attachment(BaseEntity):
    __tablename__ = "attachments"
    __table_args__ = (
        Index("ix_attachments_uploader_id", "uploader_id"),
        Index("ix_attachments_message_id", "message_id"),
        CheckConstraint("size > 0 AND size <= 10485760", name="ck_attachments_size"),
        CheckConstraint("purpose IN ('inquiry', 'document')", name="ck_attachments_purpose"),
        CheckConstraint("message_id IS NULL OR purpose = 'inquiry'", name="ck_attachment_message_purpose"),
    )

    attachment_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), nullable=False)
    uploader_id: Mapped[int] = mapped_column(ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.message_id", ondelete="RESTRICT"))
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime: Mapped[str] = mapped_column(String(100), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False, server_default="inquiry")
