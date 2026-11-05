"""기존 문의 첨부를 보존하며 문서 첨부 목적 분리."""
from alembic import op
import sqlalchemy as sa

revision = "0007_document_attachments"
down_revision = "0006_create_documents"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("attachments", sa.Column("purpose", sa.String(20), server_default="inquiry", nullable=False))
    op.create_check_constraint("ck_attachments_purpose", "attachments", "purpose IN ('inquiry', 'document')")
    op.create_check_constraint("ck_attachment_message_purpose", "attachments", "message_id IS NULL OR purpose = 'inquiry'")


def downgrade():
    op.drop_constraint("ck_attachment_message_purpose", "attachments", type_="check")
    op.drop_constraint("ck_attachments_purpose", "attachments", type_="check")
    op.drop_column("attachments", "purpose")
