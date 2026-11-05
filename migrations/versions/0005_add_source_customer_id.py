"""문의에 인증과 분리된 선택적 외부 고객 ID 추가."""
from alembic import op
import sqlalchemy as sa

revision = "0005_add_source_customer_id"
down_revision = "0004_create_inquiries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("inquiries", sa.Column("source_customer_id", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("inquiries", "source_customer_id")
