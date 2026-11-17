"""Encrypted, version-bound generated credentials for the customer's bot."""
import asyncio,json,hashlib
from cryptography.fernet import Fernet,InvalidToken
from sqlalchemy import select,update
from app.database.models import User,GuestOrder,GuestPurchaseContext

async def credentials(repo,settings,user):
    from app.web.guest_purchase import new_password,password_hash
    if not settings.encryption_key:return None
    cipher=Fernet(settings.encryption_key.encode());key=f'account_credentials209:{user.id}'
    raw=await repo.setting(key)
    if not raw:
        # Lock only first-time password generation, never ordinary menu reads.
        user=await repo.session.scalar(select(User).where(User.id==user.id).with_for_update().execution_options(populate_existing=True))
        from app.database.models import AppSetting
        row=await repo.session.get(AppSetting,key,populate_existing=True)
        raw=row.value if row else ''
    if raw:
        data=json.loads(raw)
        if data['hash']==hashlib.sha256((user.password_hash or '').encode()).hexdigest():
            try:return cipher.decrypt(data['secret'].encode()).decode()
            except InvalidToken:raise ValueError('Данные пароля недоступны. Обратитесь в поддержку.')
        # A customer changed the password: never replace it a second time.
        return None
    password=new_password();user.password_hash=await asyncio.to_thread(password_hash,password)
    user.auth_version+=1
    await repo.session.execute(update(GuestOrder).where(GuestOrder.user_id==user.id).values(secret=''))
    await repo.session.execute(update(GuestPurchaseContext).where(GuestPurchaseContext.user_id==user.id).values(secret=''))
    await repo.set_setting(key,json.dumps(dict(secret=cipher.encrypt(password.encode()).decode(),hash=hashlib.sha256(user.password_hash.encode()).hexdigest())))
    return password
