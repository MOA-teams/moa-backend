from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseEntity


class Tenant(BaseEntity):
    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    name: Mapped[str] = mapped_column(String(100), nullable=False)
