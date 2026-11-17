"""Current commercial terms for automatic labels; personal names stay personal."""
import re

def expiry_date(value):
    from datetime import timezone
    from zoneinfo import ZoneInfo
    months=('января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря')
    local=value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Europe/Moscow')) if value.tzinfo is None else value.astimezone(ZoneInfo('Europe/Moscow'))
    return f'{local.day} {months[local.month-1]} {local.year}'

DEVICE_PART = r'\d+\s*(?:устр\.?|устройство|устройства|устройств)'
TERM_PART = r'(?:год|\d+\s*(?:ч\.?|час|часа|часов|дн\.?|день|дня|дней|мес\.?|месяц|месяца|месяцев|год|года|лет))'
TERM_SEPARATOR = r'\s*[·•/—–,-]\s*'


def term_label(value):
    parts = re.split(TERM_SEPARATOR, value.strip())
    if len(parts) > 1 and all(re.fullmatch(f'{DEVICE_PART}|{TERM_PART}', part, re.I) for part in parts):
        value = next((part for part in parts if re.fullmatch(TERM_PART, part, re.I)), '')
    return '1 год' if value.casefold() in ('год', '12 месяцев', '12 мес.') else value


def automatic(value):
    value=(value or '').strip()
    if not value or re.match(r'^(?:vless(?:\s|·|$)|https?://)',value,re.I):return True
    if re.fullmatch(r'(?:Подписка(?: #\d+)?|Пробная подписка|Freeman \d+|F R E E M A N · #\d+)',value,re.I):return True
    value=re.sub(r'^(?:Подписка|Подарок)\s*[·—-]\s*','',value,flags=re.I)
    part = f'(?:{DEVICE_PART}|{TERM_PART})'
    return bool(re.fullmatch(part + f'(?:{TERM_SEPARATOR}{part})*', value, re.I))

async def current(repo,sub,key=None,term=None):
    from app.services.screens import duration
    from app.services.bot_catalog import devices_label
    tariff=await repo.tariff_by_id(sub.tariff_id) if sub.tariff_id else None
    term=term or (duration(tariff.months) if tariff else '') or await repo.setting(f'subscription_duration:{sub.id}','') or ('Пробный период' if sub.kind=='trial' else '')
    term = term_label(term)
    raw=await repo.setting(f'subscription_name:{sub.id}','') or (key.name if key else '')
    name=raw if not automatic(raw) else ('Подарок · ' if raw.startswith('Подарок ·') else '')+devices_label(sub.device_limit)+((' · '+term) if term else '')
    return name,term

async def refresh(repo,sub,key,term=None):
    if term is not None:await repo.set_setting(f'subscription_duration:{sub.id}',term)
    # Read each name independently; a personal name is never overwritten.
    stored=await repo.setting(f'subscription_name:{sub.id}','')
    name,_=await current(repo,sub,key,term)
    if automatic(stored):await repo.set_setting(f'subscription_name:{sub.id}',name)
    if key and automatic(key.name):key.name=name
    return name

async def key_row(repo,key,sub=None):
    from app.services.screens import time_remaining
    from app.services.bot_catalog import devices_label
    from app.services.vless_creation import utc
    from app.database.models import now
    raw=await repo.setting(f'subscription_name:{sub.id}','') if sub else ''
    name=raw or key.name
    expiry=sub.expires_at if sub else key.expires_at
    remaining=time_remaining(expiry)
    status='Срок истёк' if utc(expiry)<=now() else ('Осталось '+remaining)
    if key.status not in ('active','deleted') and utc(expiry)>now():status+=' · Приостановлен'
    parts=[f'№{key.id}',devices_label(sub.device_limit) if sub else '',status]
    if not automatic(name):
        room=max(0,64-len(' · '.join(p for p in parts if p))-3)
        if room:parts.insert(1,name[:min(18,room)])
    return ' · '.join(p for p in parts if p)
