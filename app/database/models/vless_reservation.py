"""Capacity and short-lived dispatch ownership for the existing I01 operation."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from . import Base, now


class VlessReservation(Base):
    __tablename__ = "vless_reservations"
    __table_args__ = (
        CheckConstraint("state IN ('reserved', 'consumed', 'released')"),
        Index(
            "ix_vless_reservations_pending",
            "server_id",
            postgresql_where=text("state = 'reserved'"),
            sqlite_where=text("state = 'reserved'"),
        ),
    )

    # No FK to the caller's uncommitted purchase or to a removable panel.
    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    server_id: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="reserved")
    lease_token: Mapped[str] = mapped_column(String(64), default="")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
