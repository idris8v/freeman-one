"""Creation journal survives rollback of the purchase/subscription transaction."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from . import Base, now


class VlessCreation(Base):
    __tablename__ = "vless_creations"
    __table_args__ = (UniqueConstraint("subscription_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Deliberately no FKs: these records must commit while their parent purchase
    # and newly allocated subscription are still uncommitted on another connection.
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    customer_ref: Mapped[str] = mapped_column(String(96))
    server_id: Mapped[int] = mapped_column(Integer)
    server_url: Mapped[str] = mapped_column(String(512))
    request: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        String(24), default="outcome_unknown", index=True
    )
    dispatch_state: Mapped[str] = mapped_column(
        String(24), default="legacy_unknown", server_default="legacy_unknown"
    )
    placement: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    result: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str] = mapped_column(String(255), default="")
    subscription_id: Mapped[int | None] = mapped_column(Integer)
    credential_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
