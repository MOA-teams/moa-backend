"""외부 문자열 ID와 계약 Enum. 내부 FK와 원본 데이터는 보존한다."""

from alembic import op
import sqlalchemy as sa

revision = "0008_public_contract"
down_revision = "0007_document_attachments"
branch_labels = None
depends_on = None

SPECS = {
    "customer": ("U-", 4, 2001),
    "agent": ("agent_", 2, 1),
    "admin": ("admin_", 2, 1),
    "inquiry": ("Q-", 4, 1001),
    "message": ("MSG-", 6, 1),
    "attachment": ("ATT-", 4, 1),
}
ENUMS = {
    "doc_type": {"policy": "정책", "faq": "FAQ", "feature_spec": "기능 명세", "release_note": "릴리즈 노트"},
    "doc_status": {"valid": "유효", "retired": "폐기"},
    "document_indexing_status": {"pending": "대기", "running": "처리 중", "completed": "완료", "failed": "실패"},
    "inquiry_category": {
        "usage": "이용 방법",
        "account": "계정",
        "billing": "결제",
        "bug": "기능 오류",
        "feature_request": "개선 요청",
        "other": "기타",
    },
    "inquiry_urgency": {"high": "높음", "normal": "보통", "low": "낮음"},
    "inquiry_status": {
        "received": "접수",
        "in_review": "담당자 확인 중",
        "needs_info": "추가 정보 필요",
        "answered": "답변 완료",
        "closed": "상담 종료",
    },
    "message_type": {
        "customer_message": "고객 메시지",
        "acknowledgement": "접수 안내",
        "information_request": "정보 수집 질문",
        "final_answer": "최종 답변",
    },
}
TABLES = {
    "users": "user_id",
    "inquiries": "inquiry_id",
    "messages": "message_id",
    "attachments": "attachment_id",
}


def rename_enums(reverse=False):
    for name, mapping in ENUMS.items():
        for old, new in mapping.items():
            if reverse:
                old, new = new, old
            op.execute(f"ALTER TYPE {name} RENAME VALUE '{old}' TO '{new}'")


def upgrade():
    connection = op.get_bind()
    # 전체 범위 검증을 먼저 수행한다. 기존 ID의 간격과 무관하게 안정된 PK 순서를 사용한다.
    for kind, (prefix, width, start) in SPECS.items():
        table = {"customer": "users", "agent": "users", "admin": "users"}.get(
            kind, kind + "s" if kind != "inquiry" else "inquiries"
        )
        condition = f" WHERE role = '{kind}'" if table == "users" else ""
        count = connection.scalar(sa.text(f"SELECT count(*) FROM {table}{condition}"))
        if start + count - 1 >= 10**width:
            raise RuntimeError(f"ID_SPACE_EXHAUSTED: {kind}")
    for table in TABLES:
        op.add_column(table, sa.Column("public_id", sa.String(32), nullable=True))
    for kind, (prefix, width, start) in SPECS.items():
        table = {"customer": "users", "agent": "users", "admin": "users"}.get(
            kind, kind + "s" if kind != "inquiry" else "inquiries"
        )
        pk = TABLES[table]
        condition = f" WHERE role = '{kind}'" if table == "users" else ""
        op.execute(
            f"WITH numbered AS (SELECT {pk}, row_number() OVER (ORDER BY {pk}) + {start - 1} AS n FROM {table}{condition}) UPDATE {table} AS t SET public_id='{prefix}' || lpad(n.n::text,{width},'0') FROM numbered n WHERE t.{pk}=n.{pk}"
        )
        count = connection.scalar(sa.text(f"SELECT count(*) FROM {table}{condition}"))
        op.execute(f"CREATE SEQUENCE public_{kind}_seq START WITH {start + count}")
    for table in TABLES:
        op.alter_column(table, "public_id", nullable=False)
        op.create_unique_constraint(f"uq_{table}_public_id", table, ["public_id"])
    op.add_column(
        "idempotency_records",
        sa.Column("contract_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.alter_column("idempotency_records", "contract_version", server_default="2")
    rename_enums()


def downgrade():
    rename_enums(reverse=True)
    # 新계약 캐시를 구버전 서버가 재생하지 않도록 해당 캐시만 제거한다. 업무 데이터는 유지한다.
    op.execute("DELETE FROM idempotency_records WHERE contract_version = 2")
    op.drop_column("idempotency_records", "contract_version")
    for table in reversed(TABLES):
        op.drop_constraint(f"uq_{table}_public_id", table, type_="unique")
        op.drop_column(table, "public_id")
    for kind in SPECS:
        op.execute(f"DROP SEQUENCE public_{kind}_seq")
