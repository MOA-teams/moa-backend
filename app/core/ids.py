"""외부 계약 ID를 DB 시퀀스로 발급한다. 내부 PK와 독립적으로 유지한다."""

from sqlalchemy import text
from sqlalchemy.exc import DontWrapMixin
from app.core.errors import APIError

ID_SPECS = {
    "customer": ("U-", 4, 2001),
    "agent": ("agent_", 2, 1),
    "admin": ("admin_", 2, 1),
    "inquiry": ("Q-", 4, 1001),
    "message": ("MSG-", 6, 1),
    "attachment": ("ATT-", 4, 1),
    "answer": ("A-", 4, 1001),
    "draft": ("DRAFT-", 6, 1),
}


class IDSpaceExhausted(APIError, DontWrapMixin):
    def __init__(self):
        super().__init__(
            503, "ID_SPACE_EXHAUSTED", "발급 가능한 ID 범위를 초과했습니다."
        )


def public_id_default(kind):
    def allocate(context):
        selected = kind
        if kind == "user":
            role = context.get_current_parameters()["role"]
            selected = getattr(role, "value", role)
        prefix, width, _ = ID_SPECS[selected]
        # selected는 코드의 고정 목록에만 존재하며 사용자 SQL 입력을 받지 않는다.
        number = context.connection.scalar(
            text(f"SELECT nextval('public_{selected}_seq')")
        )
        if number >= 10**width:
            raise IDSpaceExhausted()
        return f"{prefix}{number:0{width}d}"

    return allocate
