"""jobs.queued_at for fair round-robin scheduling"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True))
    op.execute("UPDATE jobs SET queued_at = created_at")
    op.alter_column("jobs", "queued_at", nullable=False)


def downgrade() -> None:
    op.drop_column("jobs", "queued_at")
