"""Encrypted contacts for manually issued accounts; never included in public metadata."""
import re,hmac,hashlib
from cryptography.fernet import Fernet
from app.database.models import User
from app.services.accounts import ensure_account

def phone(value):
    if not re.fullmatch(r'[+0-9 ()-]+',value.strip()):return None
    digits=re.sub(r'\D','',value)
    if len(digits)==10:digits='7'+digits
    elif len(digits)==11 and digits.startswith('8'):digits='7'+digits[1:]
    return '+'+digits if 10<=len(digits)<=15 else None

def index(settings,value):
    normalized=phone(value)
    return 'external_phone:'+hmac.new(settings.encryption_key.encode(),normalized.encode(),hashlib.sha256).hexdigest() if normalized else None

async def find(repo,settings,value):
    key=index(settings,value)
    return int(await repo.setting(key,'0')) or None if key else None

async def create(repo,settings,value):
    normalized=phone(value)
    if not normalized:raise ValueError('Укажите полный номер телефона с кодом страны')
    existing=await find(repo,settings,value)
    if existing:return await repo.session.get(User,existing)
    user=await repo.add(User(first_name='Клиент',registration_source='external'))
    await ensure_account(repo,user)
    await repo.set_setting(index(settings,value),str(user.id))
    await repo.set_setting(f'external_contact:{user.id}',Fernet(settings.encryption_key.encode()).encrypt(normalized.encode()).decode())
    return user
