"""initial schema"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.create_table(
        "assets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_symbol", sa.String(64), nullable=False),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("jesse_symbol", sa.String(64), nullable=False),
        sa.Column("start_date", TS, nullable=False),
        sa.Column("fetched_until", TS),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "provider_symbol"),
    )
    op.create_table(
        "candles",
        sa.Column("asset_id", sa.Integer, sa.ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("ts", TS, primary_key=True),
        sa.Column("open", sa.Float(53), nullable=False),
        sa.Column("high", sa.Float(53), nullable=False),
        sa.Column("low", sa.Float(53), nullable=False),
        sa.Column("close", sa.Float(53), nullable=False),
        sa.Column("volume", sa.Float(53), nullable=False),
    )
    op.execute("SELECT create_hypertable('candles', 'ts', chunk_time_interval => INTERVAL '7 days')")
    op.execute(
        "ALTER TABLE candles SET (timescaledb.compress, "
        "timescaledb.compress_segmentby = 'asset_id', timescaledb.compress_orderby = 'ts')"
    )
    op.execute("SELECT add_compression_policy('candles', INTERVAL '30 days')")
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("asset_id", sa.Integer, sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("range_start", TS, nullable=False),
        sa.Column("range_end", TS, nullable=False),
        sa.Column("requests_made", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("candles_added", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("run_seconds", sa.Float(53), nullable=False, server_default=sa.text("0")),
        sa.Column("status_detail", sa.Text),
        sa.Column("next_attempt_at", TS),
        sa.Column("attempt", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_progress_at", TS),
        sa.Column("error", sa.Text),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", TS),
        sa.Column("finished_at", TS),
    )
    op.create_index(
        "jobs_one_active_per_asset",
        "jobs",
        ["asset_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'waiting', 'paused')"),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("jobs")
    op.drop_table("candles")
    op.drop_table("assets")
