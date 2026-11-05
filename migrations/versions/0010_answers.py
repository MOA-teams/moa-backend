"""담당자 답변·종료·평가와 백엔드 문서 변경번호."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_answers"
down_revision = "0009_analysis"
branch_labels = None
depends_on = None


def times():
    return [
        sa.Column(
            n, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        )
        for n in ("created_at", "updated_at")
    ]


def fk(name, target, nullable=False, unique=False, primary_key=False):
    return sa.Column(
        name,
        sa.BigInteger(),
        sa.ForeignKey(target, ondelete="RESTRICT"),
        nullable=nullable,
        unique=unique,
        primary_key=primary_key,
    )


def upgrade():
    op.execute("CREATE SEQUENCE public_answer_seq START WITH 1001")
    op.add_column(
        "tenants",
        sa.Column(
            "knowledge_revision", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "analysis_requests", sa.Column("backend_knowledge_revision", sa.BigInteger())
    )
    op.add_column("inquiries", sa.Column("closed_at", sa.DateTime(timezone=True)))
    op.add_column("inquiries", fk("closed_by", "users.user_id", nullable=True))
    op.add_column(
        "inquiries", sa.Column("feedback_requested_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "inquiries",
        sa.Column(
            "resolution_status", sa.String(20), nullable=False, server_default="미확인"
        ),
    )
    op.create_table(
        "answers",
        sa.Column("answer_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("public_id", sa.String(32), nullable=False, unique=True),
        fk("inquiry_id", "inquiries.inquiry_id"),
        fk("message_id", "messages.message_id", unique=True),
        fk("based_on_message_id", "messages.message_id"),
        sa.Column("inquiry_revision", sa.Integer(), nullable=False),
        sa.Column("backend_knowledge_revision", sa.BigInteger(), nullable=False),
        fk("draft_id", "analysis_drafts.draft_id", nullable=True),
        sa.Column("draft_revision", sa.Integer()),
        sa.Column(
            "run_id",
            sa.String(100),
            sa.ForeignKey("analysis_runs.run_id", ondelete="RESTRICT"),
        ),
        fk("agent_id", "users.user_id", nullable=True),
        sa.Column("delivery_mode", sa.String(20), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=False),
        sa.Column("evidence", JSONB(), nullable=False),
        sa.Column("original_draft", JSONB()),
        sa.Column("original_verification", JSONB()),
        sa.Column("approval", JSONB(), nullable=False),
        sa.Column("agent_edit_ratio", sa.Numeric(5, 4)),
        sa.Column(
            "answered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "inquiry_id", "inquiry_revision", name="uq_answers_inquiry_revision"
        ),
        sa.UniqueConstraint(
            "draft_id", "based_on_message_id", name="uq_answers_draft_message"
        ),
        sa.CheckConstraint(
            "agent_edit_ratio IS NULL OR (agent_edit_ratio >= 0 AND agent_edit_ratio <= 1)",
            name="ck_answer_edit_ratio",
        ),
        *times()
    )
    op.create_table(
        "feedback",
        fk("inquiry_id", "inquiries.inquiry_id", primary_key=True),
        fk("answer_id", "answers.answer_id"),
        fk("customer_id", "users.user_id"),
        sa.Column("rating", sa.Integer()),
        sa.Column("resolved", sa.Boolean()),
        sa.Column("comment", sa.Text()),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "rating IS NULL OR (rating >= 1 AND rating <= 5)", name="ck_feedback_rating"
        ),
        *times()
    )
    # worker・조회 동기화를 포함해 검색 유효성의 실제 변경만 같은 트랜잭션으로 기록한다.
    op.execute(
        """
        CREATE FUNCTION bump_document_knowledge_revision() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' OR
               (OLD.status, OLD.review_required, OLD.searchable, OLD.source_revision, OLD.content_hash)
               IS DISTINCT FROM
               (NEW.status, NEW.review_required, NEW.searchable, NEW.source_revision, NEW.content_hash)
            THEN
                UPDATE tenants SET knowledge_revision = knowledge_revision + 1
                WHERE tenant_id = (SELECT tenant_id FROM documents WHERE doc_id = NEW.doc_id);
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """
    )
    op.execute(
        """
        CREATE TRIGGER document_knowledge_revision
        AFTER INSERT OR UPDATE ON document_versions
        FOR EACH ROW EXECUTE FUNCTION bump_document_knowledge_revision();
    """
    )


def downgrade():
    op.execute("DROP TRIGGER document_knowledge_revision ON document_versions")
    op.execute("DROP FUNCTION bump_document_knowledge_revision()")
    op.drop_table("feedback")
    op.drop_table("answers")
    for field in (
        "resolution_status",
        "feedback_requested_at",
        "closed_by",
        "closed_at",
    ):
        op.drop_column("inquiries", field)
    op.drop_column("analysis_requests", "backend_knowledge_revision")
    op.drop_column("tenants", "knowledge_revision")
    op.execute("DROP SEQUENCE public_answer_seq")
