"""고객 이탈 이력과 양방향 읽음 위치를 대화·AI 분석과 분리해 저장한다."""

from alembic import op
import sqlalchemy as sa

revision = "0010_inquiry_activity"
down_revision = "0009_analysis"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("customer_last_read_sequence", "agent_last_read_sequence"):
        op.add_column("inquiries", sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"))
    op.create_check_constraint(
        "ck_inquiries_read_sequences", "inquiries",
        "customer_last_read_sequence >= 0 AND agent_last_read_sequence >= 0",
    )
    op.create_table(
        "inquiry_session_events",
        sa.Column("event_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("inquiry_id", sa.BigInteger(), sa.ForeignKey("inquiries.inquiry_id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("type IN ('left', 'new_chat', 'returned')", name="ck_inquiry_session_events_type"),
    )
    op.create_index("ix_inquiry_session_events_inquiry_event", "inquiry_session_events", ["inquiry_id", "event_id"])


def downgrade():
    op.drop_table("inquiry_session_events")
    op.drop_constraint("ck_inquiries_read_sequences", "inquiries", type_="check")
    for name in ("agent_last_read_sequence", "customer_last_read_sequence"):
        op.drop_column("inquiries", name)
