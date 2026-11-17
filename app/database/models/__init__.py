from datetime import datetime, timezone
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class EmailDelivery(Base):
    __tablename__ = 'email_deliveries'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedup: Mapped[str] = mapped_column(String(255), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey('subscriptions.id'))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey('payments.id'))
    kind: Mapped[str] = mapped_column(String(20))
    expiry: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Record:
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class User(Record, Base):
    __tablename__ = 'users'
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True, nullable=True)
    freeman_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    account_username: Mapped[str | None] = mapped_column(String(32), unique=True)
    email: Mapped[str | None] = mapped_column(String(254), unique=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Historical column retained for safe upgrades; never used for authentication.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    registration_source: Mapped[str] = mapped_column(String(16), default='telegram')
    auth_version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(String(255), default='Пользователь')
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False)
    referrer_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    status: Mapped[str] = mapped_column(String(32), default='active')
    @property
    def display_name(self):
        from app.services.customer_identity import display_name
        return display_name(self)

    # Balance is computed from the immutable ledger, never independently mutated.


class Tariff(Record, Base):
    __tablename__ = 'tariffs'
    months: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(Integer)  # kopecks
    device_limit: Mapped[int] = mapped_column(Integer, default=1)


class TrustedCustomerDevice(Record, Base):
    __tablename__ = 'customer_trusted_devices'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    auth_version: Mapped[int] = mapped_column(Integer)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str] = mapped_column(String(512), default='')
    platform: Mapped[str] = mapped_column(String(32), default='Браузер')
    browser: Mapped[str] = mapped_column(String(32), default='Браузер')
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class GuestPurchaseContext(Record, Base):
    __tablename__ = 'guest_purchase_contexts'
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    auth_version: Mapped[int | None] = mapped_column(Integer)
    secret: Mapped[str] = mapped_column(Text, default='')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class GuestOrder(Record, Base):
    __tablename__ = 'guest_orders'
    user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    checkout_id: Mapped[int | None] = mapped_column(ForeignKey('guest_purchase_contexts.id'))
    verified_email: Mapped[str | None] = mapped_column(String(254))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey('payments.id'))
    months: Mapped[int] = mapped_column(Integer)
    renewal_sid: Mapped[int] = mapped_column(Integer, default=0)
    secret: Mapped[str] = mapped_column(Text, default='')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AccountChallenge(Record, Base):
    __tablename__ = 'account_challenges'
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[str] = mapped_column(String(16))
    email: Mapped[str | None] = mapped_column(String(254))
    user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    code_hash: Mapped[str] = mapped_column(String(64), default='')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class AuthRateLimit(Base):
    __tablename__ = 'auth_rate_limits'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[int] = mapped_column(Integer)
    count: Mapped[int] = mapped_column(Integer, default=1)


class Subscription(Record, Base):
    __tablename__ = 'subscriptions'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    tariff_id: Mapped[int | None] = mapped_column(ForeignKey('tariffs.id'))
    kind: Mapped[str] = mapped_column(String(32), default='trial')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    device_limit: Mapped[int] = mapped_column(Integer, default=1)


class Device(Record, Base):
    __tablename__ = 'devices'
    subscription_id: Mapped[int] = mapped_column(ForeignKey('subscriptions.id'), index=True)
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default='pending')
    provisioning_id: Mapped[str | None] = mapped_column(String(255))


class Payment(Record, Base):
    __tablename__ = 'payments'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey('subscriptions.id'))
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default='pending')
    provider_id: Mapped[str | None] = mapped_column(String(255), unique=True)


class BalanceTransaction(Record, Base):
    __tablename__ = 'balance_transactions'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    type: Mapped[str] = mapped_column(String(32))
    amount: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(255))


class PromoCode(Record, Base):
    __tablename__ = 'promo_codes'
    code: Mapped[str] = mapped_column(String(64), unique=True)
    effect: Mapped[str] = mapped_column(String(32))
    value: Mapped[int] = mapped_column(Integer)


class PromoActivation(Record, Base):
    __tablename__ = 'promo_activations'
    __table_args__ = (UniqueConstraint('user_id', 'promo_id'),)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey('promo_codes.id'))


class Referral(Record, Base):
    __tablename__ = 'referrals'
    inviter_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    invitee_id: Mapped[int] = mapped_column(ForeignKey('users.id'), unique=True)


class FunnelEvent(Record, Base):
    __tablename__ = 'funnel_events'
    __table_args__ = (UniqueConstraint('user_id', 'stage'),)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    stage: Mapped[str] = mapped_column(String(32), index=True)
    context: Mapped[str] = mapped_column(String(100), default='')


class Notification(Record, Base):
    __tablename__ = 'notifications'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    text: Mapped[str] = mapped_column(String(4096))
    status: Mapped[str] = mapped_column(String(32), default='pending')


class AppSetting(Base):
    __tablename__ = 'app_settings'
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(String(4096))


class AccessKey(Record, Base):
    __tablename__ = 'access_keys'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32), default='active')
    mode: Mapped[str] = mapped_column(String(32), default='mock')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Server(Record, Base):
    __tablename__ = 'servers'
    name: Mapped[str] = mapped_column(String(100))
    address: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default='not_connected')


class AdminAudit(Record, Base):
    __tablename__ = 'admin_audit'
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(255))
    details: Mapped[str] = mapped_column(String(4096))


class Campaign(Record, Base):
    __tablename__ = 'campaigns'
    actor_id: Mapped[int] = mapped_column(BigInteger)
    audience: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(String(4096))
    photo: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default='draft')


class Delivery(Record, Base):
    __tablename__ = 'deliveries'
    dedup: Mapped[str] = mapped_column(String(255), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey('campaigns.id'), index=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey('subscriptions.id'))
    expiry: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(String(4096))
    photo: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Withdrawal(Record, Base):
    __tablename__ = 'withdrawals'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    details: Mapped[str] = mapped_column(String(4096))  # authenticated encrypted payment requisites
    status: Mapped[str] = mapped_column(String(32), default='draft', index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReferralReward(Record, Base):
    __tablename__ = 'referral_rewards'
    payment_id: Mapped[int] = mapped_column(ForeignKey('payments.id'), unique=True)
    inviter_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    referred_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    payment_amount: Mapped[int] = mapped_column(Integer, default=0)
    percentage: Mapped[int] = mapped_column(Integer, default=0)
    tier: Mapped[int] = mapped_column(Integer, default=1)
    purchase_type: Mapped[str] = mapped_column(String(16), default='legacy')
    status: Mapped[str] = mapped_column(String(16), default='credited')
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey('balance_transactions.id'))


class Expense(Record, Base):
    __tablename__ = 'expenses'
    amount: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(255))


class Activity(Record, Base):
    __tablename__ = 'activities'
    dedup: Mapped[str] = mapped_column(String(255), unique=True)
    text: Mapped[str] = mapped_column(String(4096))
    queued: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Ticket(Record, Base):
    __tablename__ = 'tickets'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    status: Mapped[str] = mapped_column(String(32), default='waiting_admin', index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TicketMessage(Record, Base):
    __tablename__ = 'ticket_messages'
    ticket_id: Mapped[int] = mapped_column(ForeignKey('tickets.id'), index=True)
    role: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(String(3500))
    photo: Mapped[str | None] = mapped_column(String(512))
    document: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(128), unique=True)


class WebQuickReply(Record, Base):
    __tablename__ = 'web_quick_replies'
    title: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(String(3500), default='')
    position: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class WebSupportAttachment(Record, Base):
    __tablename__ = 'web_support_attachments'
    message_id: Mapped[int | None] = mapped_column(ForeignKey('ticket_messages.id'), index=True)
    quick_reply_id: Mapped[int | None] = mapped_column(ForeignKey('web_quick_replies.id'), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    mime_type: Mapped[str] = mapped_column(String(100))
    original_name: Mapped[str] = mapped_column(String(255))
    storage_name: Mapped[str] = mapped_column(String(255), unique=True)
    size: Mapped[int] = mapped_column(Integer)


class ChatNotice(Record, Base):
    __tablename__ = 'chat_notices'
    dedup: Mapped[str] = mapped_column(String(255), unique=True)
    target_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey('tickets.id'), index=True)
    text: Mapped[str] = mapped_column(String(4096))
    photo: Mapped[str | None] = mapped_column(String(512))
    document: Mapped[str | None] = mapped_column(String(512))
    buttons: Mapped[str] = mapped_column(String(4096), default='[]')
    status: Mapped[str] = mapped_column(String(32), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    message_id: Mapped[int | None] = mapped_column(Integer)
    media_message_id: Mapped[int | None] = mapped_column(Integer)


class ForumThread(Record, Base):
    __tablename__ = 'forum_threads'
    __table_args__ = (UniqueConstraint('chat_id', 'user_id'),)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    thread_id: Mapped[int] = mapped_column(Integer)
    control_message_id: Mapped[int | None] = mapped_column(Integer)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)


class ForumTask(Record, Base):
    __tablename__ = 'forum_tasks'
    ticket_id: Mapped[int] = mapped_column(ForeignKey('tickets.id'), unique=True)
    status: Mapped[str] = mapped_column(String(32), default='pending', index=True)


class ProxyEndpoint(Record, Base):
    __tablename__ = 'proxy_endpoints'
    name: Mapped[str] = mapped_column(String(80))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    secret: Mapped[str] = mapped_column(String(2048))  # Fernet ciphertext
    status: Mapped[str] = mapped_column(String(32), default='active')


class ProxyVisit(Record, Base):
    __tablename__ = 'proxy_visits'
    __table_args__ = (UniqueConstraint('proxy_id','user_id'),)
    proxy_id: Mapped[int] = mapped_column(ForeignKey('proxy_endpoints.id'))
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    requests: Mapped[int] = mapped_column(Integer, default=1)
    last_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProxyServer(Record, Base):
    __tablename__ = 'proxy_servers'
    name: Mapped[str] = mapped_column(String(80))
    api_url: Mapped[str] = mapped_column(String(512), unique=True)
    auth: Mapped[str] = mapped_column(String(2048))
    audience: Mapped[str] = mapped_column(String(16), default='all')
    priority: Mapped[int] = mapped_column(Integer, default=100)
    unique_ip_limit: Mapped[int] = mapped_column(Integer, default=2)
    tcp_connection_limit: Mapped[int] = mapped_column(Integer, default=10)
    status: Mapped[str] = mapped_column(String(16), default='active')
    health: Mapped[str] = mapped_column(String(16), default='unknown')
    last_error: Mapped[str] = mapped_column(String(255), default='')
    last_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_connections: Mapped[int] = mapped_column(Integer, default=0)
    active_users: Mapped[int] = mapped_column(Integer, default=0)
    remote_users: Mapped[int] = mapped_column(Integer, default=0)
    total_octets: Mapped[int] = mapped_column(BigInteger, default=0)


class ProxyCredential(Record, Base):
    __tablename__ = 'proxy_credentials'
    __table_args__ = (UniqueConstraint('server_id','remote_username'),)
    server_id: Mapped[int] = mapped_column(ForeignKey('proxy_servers.id'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    remote_username: Mapped[str] = mapped_column(String(64))
    link: Mapped[str] = mapped_column(String(4096), default='')
    status: Mapped[str] = mapped_column(String(16), default='active')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    current_connections: Mapped[int] = mapped_column(Integer, default=0)
    active_unique_ips: Mapped[int] = mapped_column(Integer, default=0)
    total_octets: Mapped[int] = mapped_column(BigInteger, default=0)
    last_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Gift(Record, Base):
    __tablename__ = 'gifts'
    sender_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    recipient_id: Mapped[int] = mapped_column(BigInteger, index=True)
    tariff_id: Mapped[int | None] = mapped_column(ForeignKey('tariffs.id'))
    key_id: Mapped[int | None] = mapped_column(ForeignKey('access_keys.id'))
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey('subscriptions.id'))
    amount: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default='draft')
    code: Mapped[str] = mapped_column(String(100), unique=True)


class VpnServer(Record, Base):
    __tablename__ = 'vpn_servers'
    name: Mapped[str] = mapped_column(String(80))
    country: Mapped[str] = mapped_column(String(80), index=True)
    api_url: Mapped[str] = mapped_column(String(512), unique=True)
    api_key: Mapped[str] = mapped_column(String(2048))
    protocol: Mapped[str] = mapped_column(String(32), default='amneziawg3')
    priority: Mapped[int] = mapped_column(Integer, default=100)
    max_clients: Mapped[int] = mapped_column(Integer, default=1000)
    status: Mapped[str] = mapped_column(String(16), default='active')
    health: Mapped[str] = mapped_column(String(16), default='unknown')
    last_error: Mapped[str] = mapped_column(String(255), default='')
    last_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_clients: Mapped[int] = mapped_column(Integer, default=0)
    online_clients: Mapped[int] = mapped_column(Integer, default=0)
    total_received: Mapped[int] = mapped_column(BigInteger, default=0)
    total_sent: Mapped[int] = mapped_column(BigInteger, default=0)
    cpu_percent: Mapped[int] = mapped_column(Integer, default=0)
    memory_percent: Mapped[int] = mapped_column(Integer, default=0)
    disk_percent: Mapped[int] = mapped_column(Integer, default=0)


class VpnCredential(Record, Base):
    __tablename__ = 'vpn_credentials'
    __table_args__ = (UniqueConstraint('subscription_id'), UniqueConstraint('server_id', 'remote_client_id'))
    server_id: Mapped[int] = mapped_column(ForeignKey('vpn_servers.id'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey('subscriptions.id'), index=True)
    access_key_id: Mapped[int | None] = mapped_column(ForeignKey('access_keys.id'))
    remote_client_id: Mapped[str] = mapped_column(String(255))
    config: Mapped[str] = mapped_column(Text)
    protocol: Mapped[str] = mapped_column(String(32), default='amneziawg3')
    status: Mapped[str] = mapped_column(String(16), default='active')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received: Mapped[int] = mapped_column(BigInteger, default=0)
    sent: Mapped[int] = mapped_column(BigInteger, default=0)
    last_handshake: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RemnawaveServer(Record, Base):
    __tablename__ = 'remnawave_servers'
    name: Mapped[str] = mapped_column(String(80))
    country: Mapped[str] = mapped_column(String(80), index=True)
    api_url: Mapped[str] = mapped_column(String(512), unique=True)
    api_token: Mapped[str] = mapped_column(String(2048))
    internal_squad_uuid: Mapped[str] = mapped_column(String(64), default='')
    priority: Mapped[int] = mapped_column(Integer, default=100)
    max_users: Mapped[int] = mapped_column(Integer, default=10000)
    status: Mapped[str] = mapped_column(String(16), default='active')
    health: Mapped[str] = mapped_column(String(16), default='unknown')
    users_total: Mapped[int] = mapped_column(Integer, default=0)
    capacity_revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default='0')
    users_online: Mapped[int] = mapped_column(Integer, default=0)
    traffic_used: Mapped[int] = mapped_column(BigInteger, default=0)
    last_error: Mapped[str] = mapped_column(String(255), default='')
    last_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VlessCredential(Record, Base):
    __tablename__ = 'vless_credentials'
    __table_args__ = (UniqueConstraint('subscription_id'), UniqueConstraint('server_id', 'remote_user_uuid'))
    server_id: Mapped[int] = mapped_column(ForeignKey('remnawave_servers.id'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey('subscriptions.id'), index=True)
    access_key_id: Mapped[int | None] = mapped_column(ForeignKey('access_keys.id'))
    remote_user_uuid: Mapped[str] = mapped_column(String(64))
    short_uuid: Mapped[str] = mapped_column(String(64), default='')
    subscription_url: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(16), default='active')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    device_limit: Mapped[int] = mapped_column(Integer, default=1)
    traffic_limit: Mapped[int] = mapped_column(BigInteger, default=0)
    traffic_used: Mapped[int] = mapped_column(BigInteger, default=0)
    last_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebUsefulLink(Record, Base):
    __tablename__ = 'web_useful_links'
    label: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(512))
    icon: Mapped[str] = mapped_column(String(32), default='link')
    position: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class WebNews(Record, Base):
    __tablename__ = 'web_news'
    title: Mapped[str] = mapped_column(String(180))
    text: Mapped[str] = mapped_column(String(2000), default='')
    target: Mapped[str] = mapped_column(String(512), default='')
    category: Mapped[str] = mapped_column(String(50), default='Обновление')
    published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class Publication(Base):
    __tablename__ = 'publications'
    id: Mapped[int] = mapped_column(ForeignKey('web_news.id'), primary_key=True)
    section: Mapped[str] = mapped_column(String(16), default='news', index=True)
    status: Mapped[str] = mapped_column(String(16), default='draft', index=True)
    content: Mapped[str] = mapped_column(Text, default='{"ops":[{"insert":"\\n"}]}')
    body: Mapped[str] = mapped_column(Text, default='')
    plain: Mapped[str] = mapped_column(Text, default='')
    media: Mapped[str] = mapped_column(Text, default='[]')
    cover: Mapped[str] = mapped_column(String(64), default='')
    slideshow: Mapped[bool] = mapped_column(Boolean, default=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    notify_clients: Mapped[bool] = mapped_column(Boolean, default=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    version: Mapped[int] = mapped_column(Integer, default=1)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey('campaigns.id'))
    link_id: Mapped[int | None] = mapped_column(ForeignKey('web_useful_links.id'))


class PublicationView(Record, Base):
    __tablename__ = 'publication_views'
    __table_args__ = (UniqueConstraint('publication_id','user_id'),)
    publication_id: Mapped[int] = mapped_column(ForeignKey('publications.id'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)


class PublicationMedia(Record, Base):
    __tablename__ = 'publication_media'
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    mime: Mapped[str] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'))


class PersonalOffer(Record, Base):
    __tablename__ = 'personal_offers'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey('campaigns.id'))
    key_id: Mapped[int | None] = mapped_column(ForeignKey('access_keys.id'))
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey('subscriptions.id'))
    usage_limit: Mapped[int | None] = mapped_column(Integer().evaluates_none(),default=1)
    use_count: Mapped[int] = mapped_column(Integer,default=0)
    no_stack: Mapped[bool] = mapped_column(Boolean,default=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    request_id: Mapped[str] = mapped_column(String(100), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    discount_type: Mapped[str] = mapped_column(String(16))
    value: Mapped[int] = mapped_column(Integer)  # percent basis points, fixed amount kopeks
    target: Mapped[str] = mapped_column(String(16))
    tariff_ids: Mapped[str] = mapped_column(Text, default='[]')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(16), default='active', index=True)
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(String(4096))
    reserved_payment_id: Mapped[int | None] = mapped_column(ForeignKey('payments.id'))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey('payments.id'))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discount_amount: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[int] = mapped_column(Integer, default=0)


class CustomerContact(Record, Base):
    __tablename__ = 'customer_contacts'
    __table_args__ = (UniqueConstraint('user_id', 'channel', 'destination'),)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    destination: Mapped[str] = mapped_column(String(255))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class OfferDelivery(Record, Base):
    __tablename__ = 'offer_deliveries'
    __table_args__ = (UniqueConstraint('offer_id', 'contact_id'),)
    offer_id: Mapped[int] = mapped_column(ForeignKey('personal_offers.id'), index=True)
    contact_id: Mapped[str] = mapped_column(String(100))
    channel: Mapped[str] = mapped_column(String(32))
    destination: Mapped[str] = mapped_column(String(255))
    telegram_job_id: Mapped[int | None] = mapped_column(ForeignKey('deliveries.id'))
    email_job_id: Mapped[int | None] = mapped_column(ForeignKey('email_deliveries.id'))
    notification_id: Mapped[int | None] = mapped_column(ForeignKey('offer_notifications.id'))
    status: Mapped[str] = mapped_column(String(20), default='queued')
    error: Mapped[str] = mapped_column(String(255), default='')


class OfferNotification(Record, Base):
    __tablename__ = 'offer_notifications'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey('personal_offers.id'))
    title: Mapped[str] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(String(4096))
    target: Mapped[str] = mapped_column(String(512))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OfferEvent(Record, Base):
    __tablename__ = 'offer_events'
    __table_args__ = (UniqueConstraint('offer_id','user_id','kind'),)
    offer_id: Mapped[int] = mapped_column(ForeignKey('personal_offers.id'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    kind: Mapped[str] = mapped_column(String(32))

class ProxyAccessDetails(Record, Base):
    __tablename__='proxy_access_details'
    credential_id:Mapped[int]=mapped_column(ForeignKey('proxy_credentials.id'),unique=True,index=True)
    source:Mapped[str]=mapped_column(String(24),default='unknown')
    subscription_id:Mapped[int|None]=mapped_column(ForeignKey('subscriptions.id'))
    payment_id:Mapped[int|None]=mapped_column(ForeignKey('payments.id'))
    plan_id:Mapped[int|None]=mapped_column(Integer)
    ip_limit:Mapped[int]=mapped_column(Integer,default=2)
    label:Mapped[str]=mapped_column(String(100),default='')

class ProxyIPSeen(Record, Base):
    __tablename__='proxy_ip_seen'
    __table_args__=(UniqueConstraint('credential_id','ip'),)
    credential_id:Mapped[int]=mapped_column(ForeignKey('proxy_credentials.id'),index=True)
    ip:Mapped[str]=mapped_column(String(48))
    last_seen:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
    active:Mapped[bool]=mapped_column(Boolean,default=True)

class ProxyDaily(Record, Base):
    __tablename__='proxy_daily'
    __table_args__=(UniqueConstraint('credential_id','day'),)
    credential_id:Mapped[int]=mapped_column(ForeignKey('proxy_credentials.id'),index=True)
    server_id:Mapped[int]=mapped_column(ForeignKey('proxy_servers.id'),index=True)
    day:Mapped[str]=mapped_column(String(10),index=True)
    octets:Mapped[int]=mapped_column(BigInteger,default=0)

class ProxyPlan(Record, Base):
    __tablename__='proxy_plans'
    name:Mapped[str]=mapped_column(String(80),default='Telegram Proxy')
    days:Mapped[int]=mapped_column(Integer,default=30)
    price:Mapped[int]=mapped_column(Integer,default=3000)
    ip_limit:Mapped[int]=mapped_column(Integer,default=2)
    enabled:Mapped[bool]=mapped_column(Boolean,default=True)

# Import additive platform tables before Base.metadata.create_all.
from .freeman_ai import ChannelIdentity, AIConversation, AIMessage, AIJob, AIOutbox, AIIdentityChallenge, AIKnowledge, AIEvent, AIBudget, AIRateLimit
from .freeman_ai import AICheckoutLink, AIOrderAttribution, AIOwnerTask
from .freeman_ai import AIOwnerImage
from .freeman_ai import AIOwnerFile
from .vless_creation import VlessCreation
from .vless_reservation import VlessReservation
from .vless_renewal import VlessRenewal
from .vless_sync import VlessSync, VlessSyncGate
