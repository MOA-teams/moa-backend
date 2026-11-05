"""상담 이탈·읽음과 최신 main의 migration 분기를 데이터 변경 없이 합친다."""

revision = "0014_inquiry_activity_merge"
down_revision = ("0010_inquiry_activity", "0013_groups_github")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
