"""Validated payloads for the existing cabinet API; field contracts are stable."""

from pydantic import BaseModel, Field


class LinkPayload(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=512)
    icon: str = Field(default="link", max_length=32)
    position: int = Field(default=100, ge=0, le=9999)
    enabled: bool = True


class NewsPayload(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    text: str = Field(default="", max_length=2000)
    target: str = Field(default="", max_length=512)
    category: str = Field(default="Обновление", min_length=1, max_length=50)
    published: bool = True


class KeyRenamePayload(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class CheckoutPayload(BaseModel):
    antig: bool = False
    expected_amount: int | None = Field(default=None, ge=0, le=100_000_000)
    months: int | None = None
    tariff_id: int | None = Field(default=None, ge=1)
    method: str = Field(pattern="^(yookassa|balance)$")
    idempotency_key: str = Field(min_length=12, max_length=80)
    use_balance: bool = False
    subscription_id: int | None = Field(default=None, ge=1)


class TopupPayload(BaseModel):
    amount: int = Field(ge=100000, le=10_000_000)


class WithdrawalPayload(BaseModel):
    bank: str = Field(min_length=2, max_length=100)
    owner_name: str = Field(min_length=3, max_length=120)
    method: str = Field(pattern="^(phone|card)$")
    account: str = Field(min_length=10, max_length=32)
    amount: int = Field(gt=0, le=100_000_000)

    request_id: str | None = Field(default=None, min_length=10, max_length=64, pattern=r"^[a-zA-Z0-9-]+$")


class AutoRenewPayload(BaseModel):
    enabled: bool
    months: int = Field(default=1)
    tariff_id: int | None = Field(default=None, ge=1)


class GiftPayload(BaseModel):
    recipient: str = Field(min_length=1, max_length=64)


class ProxyCheckoutPayload(BaseModel):
    method: str = Field(pattern="^(yookassa|balance)$")
    idempotency_key: str = Field(min_length=12, max_length=80)


class ProfileEmailPayload(BaseModel):
    email: str = Field(min_length=5, max_length=254)


class BotLoginPayload(BaseModel):
    login: str = Field(min_length=5, max_length=24)
    code: str = Field(min_length=8, max_length=100)


class AttachmentPayload(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=100)
    data: str = Field(min_length=1, max_length=30_000_000)


class SupportMessagePayload(BaseModel):
    ticket_id: int | None = None
    text: str = Field(default="", max_length=3500)
    quick_reply_id: int | None = None
    attachments: list[AttachmentPayload] = Field(default_factory=list, max_length=6)
    client_mode: bool = False
    request_id: str = Field(default="", max_length=64, pattern=r"^[a-zA-Z0-9-]*$")
    response_status: str = Field(default="waiting_client", max_length=32)


class SupportStatusPayload(BaseModel):
    status: str = Field(max_length=32)


class SupportFlagsPayload(BaseModel):
    pinned: bool | None = None
    important: bool | None = None


class QuickReplyPayload(BaseModel):
    audience: str = Field(default="operator", pattern="^(operator|customer)$")
    remove_attachment_ids: list[int] = Field(default_factory=list, max_length=6)
    title: str = Field(min_length=1, max_length=100)
    text: str = Field(default="", max_length=3500)
    position: int = Field(default=100, ge=0, le=9999)
    enabled: bool = True
    attachments: list[AttachmentPayload] = Field(default_factory=list, max_length=6)


class AutoreplyPayload(BaseModel):
    enabled: bool
    text: str = Field(default="", max_length=3500)
