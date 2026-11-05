"""변경 이력·재검토 큐와 Issue 생성 메타데이터. 기존 원본은 변경하지 않는다."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016_admin_change_reviews"
down_revision = "0015_tenant_response_policies"
branch_labels = None
depends_on = None


def timestamps():
    return [sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()) for name in ("created_at", "updated_at")]


def upgrade():
    op.add_column("issue_jobs", sa.Column("issue_metadata", JSONB()))
    op.add_column("issue_jobs", sa.Column("readiness_reason", sa.String(100)))
    op.create_table(
        "push_events",
        sa.Column("event_id", sa.BigInteger(), sa.ForeignKey("github_events.event_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("integration_id", sa.String(80), sa.ForeignKey("github_integrations.integration_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("github_jobs.job_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("repository", sa.String(200), nullable=False),
        sa.Column("branch", sa.String(200), nullable=False),
        sa.Column("before_sha", sa.String(40)), sa.Column("after_sha", sa.String(40)),
        sa.Column("security_level", sa.Integer(), nullable=False),
        sa.Column("include_paths", JSONB(), nullable=False), sa.Column("exclude_patterns", JSONB(), nullable=False),
        sa.Column("document_bindings", JSONB(), nullable=False), sa.Column("commit_info", JSONB()),
        sa.Column("changes", JSONB(), nullable=False), sa.Column("summary", sa.Text()),
        sa.Column("complete", sa.Boolean(), nullable=False), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_code", sa.String(100)), sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("lease_until", sa.DateTime(timezone=True)), sa.Column("lease_token", sa.String(32)),
        *timestamps(),
    )
    op.create_index("ix_push_events_due", "push_events", ["status", "available_at"])
    op.create_index("ix_push_events_tenant", "push_events", ["tenant_id", "event_id"])
    op.create_table(
        "review_items",
        sa.Column("review_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("public_id", sa.String(40), nullable=False, unique=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("target_type", sa.String(20), nullable=False), sa.Column("target_id", sa.String(40), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=False),
        sa.Column("inquiry_id", sa.BigInteger(), sa.ForeignKey("inquiries.inquiry_id", ondelete="RESTRICT")),
        sa.Column("document_id", sa.BigInteger(), sa.ForeignKey("documents.doc_id", ondelete="RESTRICT")),
        sa.Column("push_event_id", sa.BigInteger(), sa.ForeignKey("push_events.event_id", ondelete="RESTRICT")),
        sa.Column("source", sa.String(30), nullable=False), sa.Column("cause_keys", JSONB(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False), sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("current_text", sa.Text(), nullable=False), sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("suggestion_status", sa.String(30), nullable=False), sa.Column("suggestion_error", sa.String(100)),
        sa.Column("analysis_request_id", sa.String(36), sa.ForeignKey("analysis_requests.request_id", ondelete="RESTRICT")),
        sa.Column("suggested_draft_id", sa.BigInteger(), sa.ForeignKey("analysis_drafts.draft_id", ondelete="RESTRICT")),
        sa.Column("suggested_text", sa.Text()), sa.Column("decision", sa.String(20)),
        sa.Column("decided_by", sa.BigInteger(), sa.ForeignKey("users.user_id", ondelete="RESTRICT")),
        sa.Column("result", JSONB()), sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "target_type", "target_id", "target_version", name="uq_review_target_version"),
        *timestamps(),
    )
    op.create_index("ix_review_items_tenant_status", "review_items", ["tenant_id", "status", "review_id"])


def downgrade():
    op.drop_table("review_items")
    op.drop_table("push_events")
    op.drop_column("issue_jobs", "readiness_reason")
    op.drop_column("issue_jobs", "issue_metadata")
