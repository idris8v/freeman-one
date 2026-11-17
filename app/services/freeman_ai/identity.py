"""Verified channel possession != ownership of an existing customer account."""
import secrets
from datetime import timedelta
from sqlalchemy import select
from app.database.models import User,CustomerContact,ChannelIdentity,AIIdentityChallenge,AIEvent,now
from app.services.customer_identity import lock_nonce
from .security import fingerprint,encrypt,aware,channel_subject

async def channel_identity(session,settings,channel,subject,seen_at=None):
    subject=channel_subject(channel,subject);digest=fingerprint(settings,channel+':'+subject)
    await lock_nonce(session,'ai:identity:'+digest)
    identity=await session.scalar(select(ChannelIdentity).where(ChannelIdentity.channel==channel,ChannelIdentity.subject_hash==digest))
    if not identity:
        identity=ChannelIdentity(channel=channel,subject_hash=digest,encrypted_subject=encrypt(settings,subject),last_seen_at=seen_at or now());session.add(identity);await session.flush()
    seen_at=seen_at or now()
    if not identity.last_seen_at or aware(identity.last_seen_at)<seen_at:identity.last_seen_at=seen_at
    await session.flush()
    # Existing primary Telegram ownership is authoritative; never infer WhatsApp ownership from an unverified contact.
    if channel=='telegram' and not identity.contact_id:
        user=await session.scalar(select(User).where(User.telegram_id==int(subject),User.status=='active'))
        if user:await bind(session,settings,identity,user,'existing_telegram')
    return identity

async def customer(session,identity):
    contact=await session.get(CustomerContact,identity.contact_id) if identity.contact_id else None
    if not contact or not contact.enabled or not contact.verified:return None
    user=await session.get(User,contact.user_id)
    return user if user and user.status=='active' else None

async def bind(session,settings,identity,user,method):
    if user.status!='active':raise PermissionError('Account unavailable')
    await lock_nonce(session,'ai:identity-link:'+str(identity.id))
    await session.refresh(identity)
    contact_before=await session.get(CustomerContact,identity.contact_id) if identity.contact_id else None
    if contact_before and contact_before.user_id!=user.id:raise PermissionError('Identity already belongs to another customer')
    old=await customer(session,identity)
    if old and old.id!=user.id:raise PermissionError('Identity already belongs to another customer')
    # CustomerContact is reused, while its channel destination contains an opaque binding reference.
    contact=await session.scalar(select(CustomerContact).where(CustomerContact.user_id==user.id,CustomerContact.channel==identity.channel,CustomerContact.destination=='ai:'+identity.subject_hash))
    if not contact:
        contact=CustomerContact(user_id=user.id,channel=identity.channel,destination='ai:'+identity.subject_hash,verified=True,enabled=True);session.add(contact);await session.flush()
    contact.verified=True;contact.enabled=True;identity.contact_id=contact.id
    from app.database.models import AIConversation
    from sqlalchemy import update
    await session.execute(update(AIConversation).where(AIConversation.identity_id==identity.id,AIConversation.user_id.is_(None)).values(user_id=user.id))
    session.add(AIEvent(kind='identity.linked',name=method,details=__import__('json').dumps({'identity_id':identity.id,'user_id':user.id})))

async def issue_challenge(session,settings,identity):
    raw=secrets.token_urlsafe(32)
    session.add(AIIdentityChallenge(identity_id=identity.id,token_hash=fingerprint(settings,'link:'+raw),expires_at=now()+timedelta(minutes=10)))
    return raw

async def consume_challenge(session,settings,user,raw):
    digest=fingerprint(settings,'link:'+raw);await lock_nonce(session,'ai:link:'+digest)
    row=await session.scalar(select(AIIdentityChallenge).where(AIIdentityChallenge.token_hash==digest).with_for_update())
    if not row or row.used_at or aware(row.expires_at)<=now():raise ValueError('Подтверждение истекло или уже использовано.')
    await lock_nonce(session,'ai:identity-link:'+str(row.identity_id))
    identity=await session.get(ChannelIdentity,row.identity_id,populate_existing=True)
    await bind(session,settings,identity,user,'authenticated_account');row.used_at=now()
    return identity
