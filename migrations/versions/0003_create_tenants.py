"""기업과 계정의 선택적 기업 소속 추가."""
from alembic import op
import sqlalchemy as sa

revision = "0003_create_tenants"
down_revision = "0002_create_refresh_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenants"),
    )
    op.add_column("users", sa.Column("tenant_id", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_users_tenant_id_tenants", "users", "tenants", ["tenant_id"], ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_constraint("fk_users_tenant_id_tenants", "users", type_="foreignkey")
    op.drop_column("users", "tenant_id")
    op.drop_table("tenants")
