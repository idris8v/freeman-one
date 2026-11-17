"""Local device names from Google's supported-device catalogue; no remote lookup."""
import json,re
from functools import lru_cache
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

@lru_cache(maxsize=1)
def models():
    return json.loads((Path(__file__).parents[1]/'config/android_models.json').read_text(encoding='utf-8'))

def name(device):
    code=str(device.get('deviceModel') or '').strip()
    key=re.sub(r'/(?:DS|DSN)$','',code,flags=re.I).casefold()
    found=models().get(key) if key else None
    if found:
        brand,marketing=found
        return marketing if marketing.casefold().startswith(brand.casefold()) else brand+' '+marketing
    return code or str(device.get('platform') or 'Устройство')

def application(device):
    agent=str(device.get('userAgent') or '').strip()
    match=re.match(r'([\w .+-]{1,50})/([\w.+-]{1,30})',agent)
    return (match[1]+' '+match[2]) if match else agent[:100] or 'Не передано приложением'

def date(value):
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')
    except (ValueError,TypeError):return 'Не передано приложением'

def fields(device):
    result=[('Подключено',date(device.get('createdAt'))),('Обновлено',date(device.get('updatedAt'))),('Система',' '.join(str(device.get(k) or '') for k in ('platform','osVersion')).strip() or 'Не передано приложением'),('Приложение',application(device))]
    code=str(device.get('deviceModel') or '')
    if code and code!=name(device):result.insert(0,('Код модели',code))
    return result
