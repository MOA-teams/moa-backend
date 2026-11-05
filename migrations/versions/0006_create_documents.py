"""문서 원본·버전과 복구 가능한 RAG 작업 저장."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_create_documents"
down_revision = "0005_add_source_customer_id"
branch_labels = None
depends_on = None


def timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)]


def upgrade():
    op.create_table("document_counters",
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("last_number", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("last_number BETWEEN 0 AND 999", name="ck_document_counter_limit"), *timestamps())
    op.create_table("documents",
        sa.Column("doc_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("ai_source_id", sa.String(7), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tenant_id", "ai_source_id", name="uq_documents_tenant_ai_id"),
        sa.CheckConstraint("latest_version > 0", name="ck_document_latest_version"), *timestamps())
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_table("document_versions",
        sa.Column("version_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("doc_id", sa.BigInteger(), sa.ForeignKey("documents.doc_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("uploader_id", sa.BigInteger(), sa.ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("attachment_id", sa.BigInteger(), sa.ForeignKey("attachments.attachment_id", ondelete="RESTRICT"), unique=True),
        sa.Column("original_text", sa.Text()),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("doc_type", sa.Enum("policy", "faq", "feature_spec", "release_note", name="doc_type"), nullable=False),
        sa.Column("authored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Enum("valid", "retired", name="doc_status"), nullable=False),
        sa.Column("review_required", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("extracted_text", sa.Text()), sa.Column("content_hash", sa.String(64)),
        sa.Column("source_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("submission_possible", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("indexing_status", sa.Enum("pending", "running", "completed", "failed", name="document_indexing_status"), nullable=False),
        sa.Column("searchable", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("indexing_error", sa.String(200)), sa.Column("sync_error", sa.String(200)),
        sa.UniqueConstraint("doc_id", "version", name="uq_document_versions_doc_version"),
        sa.CheckConstraint("version > 0 AND source_revision > 0", name="ck_document_version_positive"),
        sa.CheckConstraint("(original_text IS NULL) <> (attachment_id IS NULL)", name="ck_document_original_source"), *timestamps())
    op.create_table("document_jobs",
        sa.Column("job_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("version_id", sa.BigInteger(), sa.ForeignKey("document_versions.version_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False), sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("barrier", postgresql.JSONB()),
        sa.Column("barrier_confirmed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("resubmissions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)), sa.Column("lease_token", sa.String(32)),
        sa.Column("poll_started_at", sa.DateTime(timezone=True)), sa.Column("last_error", sa.String(200)),
        sa.UniqueConstraint("version_id", "source_revision", name="uq_document_jobs_version_revision"), *timestamps())
    op.create_index("ix_document_jobs_due", "document_jobs", ["phase", "available_at"])


def downgrade():
    op.drop_table("document_jobs")
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_table("document_counters")
    for name in ("doc_type", "doc_status", "document_indexing_status"):
        sa.Enum(name=name).drop(op.get_bind())
