from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseEntity


class TenantResponsePolicy(BaseEntity):
    __tablename__ = "tenant_response_policies"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    policy: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)
