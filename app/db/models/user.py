from sqlalchemy import BigInteger, Enum, Identity, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import UserRole
from app.db.base import BaseEntity


class User(BaseEntity):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(BigInteger, Identity(start=1), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole, name="user_role",
            values_callable=lambda roles: [role.value for role in roles],
            validate_strings=True,
        ),
        nullable=False,
    )
