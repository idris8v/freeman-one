"""Browser-bound, expiring Telegram approval; tokens are never stored in plaintext."""
import hashlib
import secrets
from datetime import timedelta
from sqlalchemy import select, update
from app.database.models import AccountChallenge, User, now

TTL=300
COOKIE='freeman_telegram_state'
def digest(value): return hashlib.sha256(value.encode()).hexdigest()

async def initiate(session, browser_secret, referrer_id=None):
    token=secrets.token_urlsafe(24)
    # The optional context uses the existing challenge field; no customer identity is changed.
    session.add(AccountChallenge(token_hash=digest(token),code_hash=digest(browser_secret),email=str(referrer_id) if referrer_id else None,purpose='browser_telegram',expires_at=now()+timedelta(seconds=TTL)))
    await session.flush()
    return token

async def challenge(session, token):
    return await session.scalar(select(AccountChallenge).where(AccountChallenge.token_hash==digest(token),AccountChallenge.purpose=='browser_telegram',AccountChallenge.consumed.is_(False),AccountChallenge.expires_at>now()).execution_options(populate_existing=True))

async def approve(session, token, user):
    if not user.telegram_id or user.status!='active': return False
    result=await session.execute(update(AccountChallenge).where(AccountChallenge.token_hash==digest(token),AccountChallenge.purpose=='browser_telegram',AccountChallenge.consumed.is_(False),AccountChallenge.user_id.is_(None),AccountChallenge.expires_at>now()).values(user_id=user.id).execution_options(synchronize_session=False))
    return result.rowcount==1

async def consume(session, token, browser_secret):
    row=await challenge(session,token)
    if not row or not secrets.compare_digest(row.code_hash,digest(browser_secret)): raise ValueError('Запрос входа истёк. Начните снова.')
    if row.user_id is None: return None
    user=await session.get(User,row.user_id)
    if not user or user.status!='active' or not user.telegram_id: raise ValueError('Аккаунт недоступен.')
    result=await session.execute(update(AccountChallenge).where(AccountChallenge.id==row.id,AccountChallenge.consumed.is_(False),AccountChallenge.expires_at>now()).values(consumed=True).execution_options(synchronize_session=False))
    if result.rowcount!=1: raise ValueError('Запрос входа уже использован.')
    return user
