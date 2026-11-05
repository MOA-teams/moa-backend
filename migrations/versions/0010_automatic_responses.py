"""자동 응대 판단·발송 이력과 요청 시점의 문서 상태 보존."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0010_automatic_responses"
down_revision = "0009_analysis"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("analysis_requests", sa.Column("knowledge_snapshot", pg.JSONB()))
    op.create_table(
        "automatic_responses",
        sa.Column(
            "run_id",
            sa.String(100),
            sa.ForeignKey("analysis_runs.run_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "inquiry_id",
            sa.BigInteger(),
            sa.ForeignKey("inquiries.inquiry_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "based_on_message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.message_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.message_id", ondelete="RESTRICT"),
            unique=True,
        ),
        sa.Column(
            "draft_id",
            sa.BigInteger(),
            sa.ForeignKey("analysis_drafts.draft_id", ondelete="RESTRICT"),
        ),
        sa.Column("delivered_revision", sa.Integer()),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("review_reasons", pg.JSONB(), nullable=False),
        sa.Column("policy", pg.JSONB(), nullable=False),
        sa.Column("validation", pg.JSONB(), nullable=False),
        *[
            sa.Column(
                n,
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            )
            for n in ("created_at", "updated_at")
        ],
    )
    op.create_index(
        "ix_automatic_responses_inquiry", "automatic_responses", ["inquiry_id"]
    )
    op.create_index(
        "uq_automatic_response_customer_message",
        "automatic_responses",
        ["inquiry_id", "based_on_message_id"],
        unique=True,
        postgresql_where=sa.text("message_id IS NOT NULL"),
    )


def downgrade():
    op.drop_table("automatic_responses")
    op.drop_column("analysis_requests", "knowledge_snapshot")
