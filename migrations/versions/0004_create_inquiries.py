"""숫자 ID의 문의·대화·첨부와 중복 요청 기록 추가."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_create_inquiries"
down_revision = "0003_create_tenants"
branch_labels = None
depends_on = None

ENUMS = {
    "inquiry_category": ("usage", "account", "billing", "bug", "feature_request", "other"),
    "inquiry_urgency": ("high", "normal", "low"),
    "inquiry_status": ("received", "in_review", "needs_info", "answered", "closed"),
    "message_sender": ("customer", "agent", "ai_auto"),
    "message_type": ("customer_message", "acknowledgement", "information_request", "final_answer"),
}


def timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def enum(name):
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def upgrade():
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(op.get_bind())
    op.create_table(
        "inquiries",
        sa.Column("inquiry_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), sa.ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("customer_category", enum("inquiry_category"), nullable=False),
        sa.Column("env", postgresql.JSONB(), nullable=False),
        sa.Column("status", enum("inquiry_status"), nullable=False),
        sa.Column("category", enum("inquiry_category")),
        sa.Column("urgency", enum("inquiry_urgency")),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("last_message_sequence", sa.BigInteger(), nullable=False),
        *timestamps(),
        sa.CheckConstraint("revision > 0 AND last_message_sequence >= 0", name="ck_inquiries_revision_sequence"),
    )
    op.create_index("ix_inquiries_tenant_created_id", "inquiries", ["tenant_id", "created_at", "inquiry_id"])
    op.create_index("ix_inquiries_customer_id", "inquiries", ["customer_id"])
    op.create_table(
        "messages",
        sa.Column("message_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("inquiry_id", sa.BigInteger(), sa.ForeignKey("inquiries.inquiry_id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("sender", enum("message_sender"), nullable=False),
        sa.Column("message_type", enum("message_type"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("inquiry_id", "sequence", name="uq_messages_inquiry_sequence"),
        sa.CheckConstraint("sequence > 0", name="ck_messages_sequence"),
    )
    op.create_table(
        "attachments",
        sa.Column("attachment_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("uploader_id", sa.BigInteger(), sa.ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("message_id", sa.BigInteger(), sa.ForeignKey("messages.message_id", ondelete="RESTRICT")),
        sa.Column("storage_key", sa.String(512), nullable=False, unique=True),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("mime", sa.String(100), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        *timestamps(),
        sa.CheckConstraint("size > 0 AND size <= 10485760", name="ck_attachments_size"),
    )
    op.create_index("ix_attachments_uploader_id", "attachments", ["uploader_id"])
    op.create_index("ix_attachments_message_id", "attachments", ["message_id"])
    op.create_table(
        "idempotency_records",
        sa.Column("record_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.String(200), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response", postgresql.JSONB()),
        *timestamps(),
        sa.UniqueConstraint("user_id", "path", "key_hash", name="uq_idempotency_user_path_key"),
    )


def downgrade():
    for table in ("idempotency_records", "attachments", "messages", "inquiries"):
        op.drop_table(table)
    for name, values in reversed(list(ENUMS.items())):
        postgresql.ENUM(*values, name=name).drop(op.get_bind())
