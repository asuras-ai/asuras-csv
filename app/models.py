from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TS = DateTime(timezone=True)
ACTIVE_STATUSES = ("queued", "running", "waiting", "paused")


class Base(DeclarativeBase):
    pass


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("provider", "provider_symbol"),)
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    provider_symbol: Mapped[str] = mapped_column(String(64))
    asset_class: Mapped[str] = mapped_column(String(16))
    jesse_symbol: Mapped[str] = mapped_column(String(64))
    start_date: Mapped[datetime] = mapped_column(TS)
    fetched_until: Mapped[datetime | None] = mapped_column(TS)
    enabled: Mapped[bool] = mapped_column(default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class CandleRow(Base):
    __tablename__ = "candles"

    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    open: Mapped[float] = mapped_column(Float(53))
    high: Mapped[float] = mapped_column(Float(53))
    low: Mapped[float] = mapped_column(Float(53))
    close: Mapped[float] = mapped_column(Float(53))
    volume: Mapped[float] = mapped_column(Float(53))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "jobs_one_active_per_asset",
            "asset_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'waiting', 'paused')"),
        ),
    )
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    priority: Mapped[int]
    range_start: Mapped[datetime] = mapped_column(TS)
    range_end: Mapped[datetime] = mapped_column(TS)
    requests_made: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    candles_added: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    run_seconds: Mapped[float] = mapped_column(Float(53), default=0.0, server_default=text("0"))
    status_detail: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(TS)
    attempt: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    last_progress_at: Mapped[datetime | None] = mapped_column(TS)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    queued_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
