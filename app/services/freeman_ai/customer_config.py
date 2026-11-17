"""Independent customer provider profile. Never fall back to the owner's key."""
from urllib.parse import urlsplit
from app.database.repositories import SQLRepository

class CustomerSettings:
    def __init__(self, base): self.base=base
    def __getattr__(self, name):
        mapping={"freeman_ai_openai_key":"freeman_customer_ai_openai_key", "freeman_ai_base_url":"freeman_customer_ai_base_url", "freeman_ai_model":"freeman_customer_ai_model"}
        return getattr(self.base,mapping.get(name,name),'') if name in mapping else getattr(self.base,name)

def configured(settings):
    base=getattr(settings,'freeman_customer_ai_base_url','')
    try:
        url=urlsplit(base)
        return bool(url.scheme=='https' and url.hostname and not url.username and not url.password and not url.query and not url.fragment and not any(v.isspace() for v in base) and str(getattr(settings,'freeman_customer_ai_openai_key','') or '').strip() and str(getattr(settings,'freeman_customer_ai_model','') or '').strip())
    except ValueError:return False

async def enabled(session,settings):
    from .v21.integration import active
    return bool(await active(session) and getattr(settings,'freeman_customer_ai_enabled',False) and configured(settings) and await SQLRepository(session).setting('freeman_customer_ai_enabled','0')=='1')

async def status(session,settings):
    return dict(configured=configured(settings),enabled=await enabled(session,settings),allowed=bool(getattr(settings,'freeman_customer_ai_enabled',False)),model=getattr(settings,'freeman_customer_ai_model','') or None,telegram_enabled=bool(getattr(settings,'freeman_customer_ai_telegram_enabled',False)),provider='Отдельное подключение клиентского агента',knowledge='shared_approved')
