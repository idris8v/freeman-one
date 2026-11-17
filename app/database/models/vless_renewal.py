"""Renewal intent and remote progress survive rollback of the paid transaction."""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from . import Base, now


class VlessRenewal(Base):
    __tablename__ = "vless_renewals"
    __table_args__ = (
        Index(
            "uq_vless_renewal_pending_subscription",
            "subscription_id",
            unique=True,
            postgresql_where=text("state NOT IN ('applied', 'failed')"),
            sqlite_where=text("state NOT IN ('applied', 'failed')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # No FKs: the journal commits independently while the caller holds locks on
    # these rows. Financial/local updates and state='applied' commit together.
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    subscription_id: Mapped[int] = mapped_column(Integer, index=True)
    credential_id: Mapped[int] = mapped_column(Integer)
    key_id: Mapped[int] = mapped_column(Integer)
    server_id: Mapped[int] = mapped_column(Integer)
    server_url: Mapped[str] = mapped_column(String(512))
    remote_identity: Mapped[str] = mapped_column(String(128))
    request: Mapped[str] = mapped_column(Text)
    base_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    base_device_limit: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    device_limit: Mapped[int] = mapped_column(Integer)
    target_status: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(24), index=True)
    patch_state: Mapped[str] = mapped_column(String(24), default="not_started")
    enable_state: Mapped[str] = mapped_column(String(24), default="not_started")
    last_error: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
