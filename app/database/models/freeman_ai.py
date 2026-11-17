"""Additive AI workflow tables; User remains the sole permanent customer."""
from . import Base, Record, now
from datetime import datetime
from sqlalchemy import String, Integer, ForeignKey, DateTime, Text, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

class ChannelIdentity(Record, Base):
    __tablename__='ai_channel_identities'
    __table_args__=(UniqueConstraint('channel','subject_hash'),)
    channel:Mapped[str]=mapped_column(String(24))
    subject_hash:Mapped[str]=mapped_column(String(64))
    encrypted_subject:Mapped[str]=mapped_column(Text)
    contact_id:Mapped[int|None]=mapped_column(ForeignKey('customer_contacts.id'),unique=True)
    last_seen_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)

class AIConversation(Record, Base):
    __tablename__='ai_conversations'
    user_id:Mapped[int|None]=mapped_column(ForeignKey('users.id'),index=True)
    identity_id:Mapped[int|None]=mapped_column(ForeignKey('ai_channel_identities.id'),index=True)
    channel:Mapped[str]=mapped_column(String(24))
    browser_hash:Mapped[str|None]=mapped_column(String(64),index=True)
    mode:Mapped[str]=mapped_column(String(16),default='ai',index=True)
    specialist:Mapped[str]=mapped_column(String(24),default='sales')
    generation:Mapped[int]=mapped_column(Integer,default=0)
    ticket_id:Mapped[int|None]=mapped_column(ForeignKey('tickets.id'))
    summary:Mapped[str]=mapped_column(Text,default='')
    updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,index=True)

class AIMessage(Record, Base):
    __tablename__='ai_messages'
    conversation_id:Mapped[int]=mapped_column(ForeignKey('ai_conversations.id'),index=True)
    source_hash:Mapped[str]=mapped_column(String(64),unique=True)
    generation:Mapped[int]=mapped_column(Integer,default=0)
    role:Mapped[str]=mapped_column(String(16))
    # Redacted content only. Connection keys are handled before storage/model invocation.
    text:Mapped[str]=mapped_column(Text)
    actions:Mapped[str]=mapped_column(Text,default='[]')

class AIJob(Record, Base):
    __tablename__='ai_jobs'
    message_id:Mapped[int]=mapped_column(ForeignKey('ai_messages.id'),unique=True)
    generation:Mapped[int]=mapped_column(Integer)
    status:Mapped[str]=mapped_column(String(32),default='pending',index=True)
    attempts:Mapped[int]=mapped_column(Integer,default=0)
    lease:Mapped[str|None]=mapped_column(String(64))
    lease_until:Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    error_code:Mapped[str]=mapped_column(String(64),default='')

class AIOutbox(Record, Base):
    __tablename__='ai_outbox'
    message_id:Mapped[int]=mapped_column(ForeignKey('ai_messages.id'),unique=True)
    status:Mapped[str]=mapped_column(String(32),default='pending',index=True)
    attempts:Mapped[int]=mapped_column(Integer,default=0)
    error_code:Mapped[str]=mapped_column(String(64),default='')
    lease_until:Mapped[datetime|None]=mapped_column(DateTime(timezone=True))

class AIIdentityChallenge(Record, Base):
    __tablename__='ai_identity_challenges'
    identity_id:Mapped[int]=mapped_column(ForeignKey('ai_channel_identities.id'))
    token_hash:Mapped[str]=mapped_column(String(64),unique=True)
    expires_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
    used_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True))

class AIKnowledge(Record, Base):
    __tablename__='ai_knowledge'
    title:Mapped[str]=mapped_column(String(160))
    text:Mapped[str]=mapped_column(Text)
    enabled:Mapped[bool]=mapped_column(Boolean,default=False)
    version:Mapped[int]=mapped_column(Integer,default=1)
    updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)

class AIEvent(Record, Base):
    __tablename__='ai_events'
    conversation_id:Mapped[int|None]=mapped_column(ForeignKey('ai_conversations.id'),index=True)
    kind:Mapped[str]=mapped_column(String(40))
    name:Mapped[str]=mapped_column(String(64),default='')
    status:Mapped[str]=mapped_column(String(32),default='ok')
    # Safe, structured metadata only; no customer text, tools arguments or credentials.
    details:Mapped[str]=mapped_column(Text,default='{}')

class AIBudget(Record, Base):
    __tablename__='ai_budgets'
    day:Mapped[str]=mapped_column(String(10),unique=True)
    reserved:Mapped[int]=mapped_column(Integer,default=0)

class AIRateLimit(Record, Base):
    __tablename__='ai_rate_limits'
    bucket:Mapped[str]=mapped_column(String(64),unique=True)
    count:Mapped[int]=mapped_column(Integer,default=0)

class AICheckoutLink(Record, Base):
    __tablename__='ai_checkout_links'
    conversation_id:Mapped[int]=mapped_column(ForeignKey('ai_conversations.id'),index=True)
    token_hash:Mapped[str]=mapped_column(String(64),unique=True)
    renewal:Mapped[bool]=mapped_column(Boolean,default=False)
    expires_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
    clicked_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True))

class AIOrderAttribution(Record, Base):
    __tablename__='ai_order_attribution'
    link_id:Mapped[int]=mapped_column(ForeignKey('ai_checkout_links.id'),index=True)
    order_id:Mapped[int]=mapped_column(ForeignKey('guest_orders.id'),unique=True)

class AIOwnerTask(Record, Base):
    __tablename__='ai_owner_tasks'
    owner_id:Mapped[int]=mapped_column(ForeignKey('users.id'),index=True)
    conversation_id:Mapped[int]=mapped_column(ForeignKey('ai_conversations.id'))
    request_key:Mapped[str]=mapped_column(String(64),unique=True)
    prompt:Mapped[str]=mapped_column(Text)
    due_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),index=True)
    status:Mapped[str]=mapped_column(String(24),default='scheduled',index=True)
    job_id:Mapped[int|None]=mapped_column(ForeignKey('ai_jobs.id'))
    finished_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True))

class AIOwnerImage(Record, Base):
    __tablename__='ai_owner_images'
    message_id:Mapped[int]=mapped_column(ForeignKey('ai_messages.id'),index=True)
    encrypted_data:Mapped[str]=mapped_column(Text)

class AIOwnerFile(Record,Base):
    __tablename__='ai_owner_files'
    message_id:Mapped[int]=mapped_column(ForeignKey('ai_messages.id'),index=True)
    name:Mapped[str]=mapped_column(String(120))
    mime:Mapped[str]=mapped_column(String(120))
    size:Mapped[int]=mapped_column(Integer)
    encrypted_data:Mapped[str]=mapped_column(Text)
