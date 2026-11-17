"""Durable read-only reconciliation queue and cross-worker panel dispatch gate."""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from . import Base, now


class VlessSync(Base):
    __tablename__ = "vless_sync"
    __table_args__ = (Index("ix_vless_sync_due", "next_attempt_at", "server_id"),)

    # Namespaces distinguish an orphaned I01 operation from an associated key.
    # No FKs: observations/leases must survive rollback and local deletion.
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    server_id: Mapped[int] = mapped_column(Integer)
    credential_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    creation_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    desired: Mapped[str] = mapped_column(Text, default="{}")
    observed: Mapped[str | None] = mapped_column(Text)
    drift: Mapped[str] = mapped_column(Text, default="[]")
    outcome: Mapped[str] = mapped_column(String(24), default="pending")
    last_error: Mapped[str] = mapped_column(String(255), default="")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remote_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )
    failures: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str] = mapped_column(String(64), default="")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VlessSyncGate(Base):
    __tablename__ = "vless_sync_gates"

    server_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_request_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )
    lease_token: Mapped[str] = mapped_column(String(64), default="")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
