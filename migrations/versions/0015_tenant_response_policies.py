"""Persist independent tenant question/answer delivery settings."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0015_tenant_response_policies"
down_revision = "0014_inquiry_activity_merge"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tenant_response_policies",
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("policy", pg.JSONB(), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade():
    op.drop_table("tenant_response_policies")
