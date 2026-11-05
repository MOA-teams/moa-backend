"""영속 분석 요청·run 결과·초안·수동 분류 이력."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0009_analysis"
down_revision = "0008_public_contract"
branch_labels = None
depends_on = None


def times():
    return [
        sa.Column(
            n, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        )
        for n in ("created_at", "updated_at")
    ]


def upgrade():
    op.execute("CREATE SEQUENCE public_draft_seq START WITH 1")
    op.add_column("inquiries", sa.Column("latest_analysis_request_id", sa.String(36)))
    op.add_column(
        "inquiries",
        sa.Column(
            "classification_manual",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column("inquiries", sa.Column("processing_stage", sa.String(100)))
    op.create_table(
        "analysis_requests",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column(
            "inquiry_id",
            sa.BigInteger(),
            sa.ForeignKey("inquiries.inquiry_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("inquiry_revision", sa.Integer(), nullable=False),
        sa.Column(
            "based_on_message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.message_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot", pg.JSONB(), nullable=False),
        sa.Column("review_only", sa.Boolean(), nullable=False),
        sa.Column(
            "run_status",
            sa.Enum("대기", "처리 중", "완료", "실패", name="pipeline_run_status"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("run_id", sa.String(100)),
        sa.Column("current_stage", sa.String(100)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("resubmissions", sa.Integer(), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(32)),
        *times(),
    )
    op.create_index("ix_analysis_due", "analysis_requests", ["phase", "available_at"])
    op.create_index("ix_analysis_inquiry", "analysis_requests", ["inquiry_id"])
    op.create_table(
        "analysis_runs",
        sa.Column("run_id", sa.String(100), primary_key=True),
        sa.Column(
            "request_id",
            sa.String(36),
            sa.ForeignKey("analysis_requests.request_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("superseded", sa.Boolean(), nullable=False),
        sa.Column("result_hash", sa.String(64)),
        sa.Column("result", pg.JSONB()),
        sa.Column("response", pg.JSONB()),
        *times(),
    )
    op.create_index("ix_analysis_runs_request_id", "analysis_runs", ["request_id"])
    op.create_table(
        "analysis_drafts",
        sa.Column("draft_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("public_id", sa.String(32), nullable=False, unique=True),
        sa.Column(
            "run_id",
            sa.String(100),
            sa.ForeignKey("analysis_runs.run_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content", pg.JSONB(), nullable=False),
        sa.Column("review_reasons", pg.JSONB(), nullable=False),
        *times(),
    )
    op.create_table(
        "classification_history",
        sa.Column("history_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "inquiry_id",
            sa.BigInteger(),
            sa.ForeignKey("inquiries.inquiry_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            sa.BigInteger(),
            sa.ForeignKey("users.user_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("before", pg.JSONB(), nullable=False),
        sa.Column("after", pg.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        *times(),
    )
    op.create_index(
        "ix_classification_history_inquiry_id", "classification_history", ["inquiry_id"]
    )


def downgrade():
    for table in (
        "classification_history",
        "analysis_drafts",
        "analysis_runs",
        "analysis_requests",
    ):
        op.drop_table(table)
    op.execute("DROP TYPE pipeline_run_status")
    for name in (
        "processing_stage",
        "classification_manual",
        "latest_analysis_request_id",
    ):
        op.drop_column("inquiries", name)
    op.execute("DROP SEQUENCE public_draft_seq")
