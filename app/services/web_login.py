"""Short-lived, single-use web credentials issued inside the Telegram bot."""

import hashlib
import hmac
import secrets
import time

from sqlalchemy import update

from app.database.models import AppSetting, User


LOGIN_TTL_SECONDS = 600


def login_name(user_id: int) -> str:
    return f'FRM-{user_id:06d}'


def _setting_key(user_id: int) -> str:
    return f'web_login:{user_id}'


async def issue(repo, user_id: int) -> str:
    code = secrets.token_urlsafe(18)
    digest = hashlib.sha256(code.encode()).hexdigest()
    user=await repo.session.get(User,user_id)
    await repo.set_setting(_setting_key(user_id), f'{int(time.time()) + LOGIN_TTL_SECONDS}:{user.auth_version}:{digest}')
    return code


async def consume(session, user_id: int, code: str) -> bool:
    if not code or len(code) > 100:
        return False
    key = _setting_key(user_id)
    row = await session.get(AppSetting, key)
    if not row:
        return False
    try:
        parts=row.value.split(':')
        expiry_text,version,digest=parts if len(parts)==3 else (parts[0],'0',parts[1])
        user=await session.get(User,user_id)
        valid = bool(user) and int(version)==user.auth_version and int(expiry_text) >= time.time() and hmac.compare_digest(
            digest, hashlib.sha256(code.encode()).hexdigest())
    except (ValueError, AttributeError, IndexError):
        return False
    if not valid:
        return False
    # Conditional update makes consumption single-use even under concurrent requests.
    result = await session.execute(update(AppSetting).where(
        AppSetting.key == key, AppSetting.value == row.value).values(value=''))
    return result.rowcount == 1


def issue_direct(settings,user,purpose="direct_cabinet"):
    """Mint without a database write: Telegram delivery must not hold SQLite's lock."""
    import json
    from cryptography.fernet import Fernet
    payload={'purpose':purpose,'user_id':user.id,'version':user.auth_version,'nonce':secrets.token_urlsafe(24)}
    return Fernet(settings.encryption_key.encode()).encrypt(json.dumps(payload).encode()).decode()

async def consume_direct(session,settings,token,purpose="direct_cabinet"):
    import json
    from cryptography.fernet import Fernet,InvalidToken
    from sqlalchemy.exc import IntegrityError
    from datetime import timedelta
    from app.database.models import AccountChallenge,now
    if not token or len(token)>1000: return None
    try:
        data=json.loads(Fernet(settings.encryption_key.encode()).decrypt(token.encode(),ttl=LOGIN_TTL_SECONDS))
        if data.get('purpose')!=purpose or not isinstance(data.get('nonce'),str): return None
        user=await session.get(User,int(data['user_id']))
        if not user or user.status!='active' or user.auth_version!=data['version'] or not user.telegram_id: return None
    except (InvalidToken,ValueError,KeyError,TypeError): return None
    try:
        async with session.begin_nested():
            session.add(AccountChallenge(token_hash=hashlib.sha256(token.encode()).hexdigest(),purpose='direct_used',user_id=user.id,consumed=True,expires_at=now()+timedelta(seconds=LOGIN_TTL_SECONDS)))
            await session.flush()
    except IntegrityError: return None
    return user.id,user.auth_version
