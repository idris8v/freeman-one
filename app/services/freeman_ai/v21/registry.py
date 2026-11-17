"""Versioned, packaged knowledge; never merges legacy database articles.

Official URLs are data, separate from generated messages. The source document
provides documentary verification; physical-device status is explicitly separate.
"""
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlsplit

URLS = {
 'incy_android':'https://play.google.com/store/apps/details?id=llc.itdev.incy',
 'incy_apple':'https://apps.apple.com/us/app/incy/id6756943388',
 'incy_apk':'https://github.com/INCY-DEV/incy-platforms',
 'happ_apple':'https://apps.apple.com/us/app/happ-proxy-utility/id6504287215',
 'happ_desktop':'https://www.happ.su/main',
 'happ_apk':'https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk',
 'rabbit_android':'https://play.google.com/store/apps/details?id=com.rabbithole.RabbitHole&hl=ru&gl=RU',
 'rabbit_apple':'https://apps.apple.com/ru/app/rabbithole-vpn-client/id6683309629',
 'speed':'https://yandex.ru/internet/',
 'bot':'https://t.me/FreemanOneBot',
 'buy':'https://freemanone.pro/buy',
 'setup':'https://freemanone.pro/connect-guide',
 'setup_macos':'https://freemanone.pro/connect-guide?os=macos&app=happ',
 'setup_android_rabbit':'https://freemanone.pro/connect-guide?os=android&app=rabbit',
 'setup_rabbit':'https://freemanone.pro/connect-guide?os=apple&app=rabbit',
 'setup_incy':'https://freemanone.pro/connect-guide?os=ios&app=incy',
 'setup_android_incy':'https://freemanone.pro/connect-guide?os=android&app=incy',
 'setup_android_happ':'https://freemanone.pro/connect-guide?os=android&app=happ',
 'setup_happ':'https://freemanone.pro/connect-guide?os=ios&app=happ',
 'setup_windows':'https://freemanone.pro/connect-guide?os=windows&app=happ',
 'setup_windows_incy':'https://freemanone.pro/connect-guide?os=windows&app=incy',
 'setup_tv':'https://freemanone.pro/connect-guide?os=tv&app=incy',
 'setup_tv_happ':'https://freemanone.pro/connect-guide?os=tv&app=happ',
 'tv':'https://docs.incy.cc/tv-relay/',
}

def cabinet_key_url(key_id, action=''):
    url=f'https://freemanone.pro/?cabinet_view=keys&managed_key={int(key_id)}'
    return url+('&key_action=antig' if action=='antig' else '')

def is_cabinet_key_url(url):
    return isinstance(url,str) and bool(re.fullmatch(
        r'https://freemanone\.pro/\?cabinet_view=keys&managed_key=[1-9][0-9]{0,9}(?:&key_action=antig)?',url))

def key_link(value):
    if not isinstance(value,str) or len(value)>2048 or any(ord(c)<33 for c in value):
        return False
    try:
        p=urlsplit(value)
        return (p.scheme=='https' and p.netloc=='sub.freemanone.top'
                and not p.query and not p.fragment
                and bool(re.fullmatch(r'/[A-Za-z0-9_-]{4,255}',p.path)))
    except ValueError:
        return False

def incy_link(value):
    if not key_link(value):
        raise ValueError('invalid_subscription_link')
    return 'incy://import/'+quote(value,safe='')

def apps(platform):
    mapping={
      'android':[('INCY','incy_android'),('Happ APK','happ_apk'),('Rabbit Hole','rabbit_android')],
      'ios':[('Happ','happ_apple'),('INCY','incy_apple'),('Rabbit Hole','rabbit_apple')],
      'windows':[('Happ','happ_desktop'),('INCY (pre-alpha)','incy_apk')],
      'macos':[('Happ','happ_desktop'),('INCY (pre-alpha)','incy_apk')],
      'android-tv':[('INCY','incy_android')],
      'apple-tv':[('INCY','incy_apple')],
    }
    if platform not in mapping:
        raise ValueError('unknown_platform')
    return [dict(name=n,url=URLS[k],document_verified=True,device_verified=False)
            for n,k in mapping[platform]]

@lru_cache(maxsize=1)
def articles():
    rows=json.loads(Path(__file__).with_name('knowledge.json').read_text())
    return tuple(a for a in rows if a['active'] and a['version']=='2.1')

def search(query, limit=5, *, rows=None, context=''):
    rows = tuple(articles() if rows is None else rows)
    tokens = set(re.findall(r'[\w-]{3,}', query.casefold()))
    context_tokens = set(re.findall(r'[\w-]{3,}', context.casefold())) - tokens

    # Article IDs in the index are stable and can open any active article.
    requested = [article for article in rows if article['id'].casefold() in tokens]
    if requested:
        return requested[:limit]

    from .intents import messenger_issue
    priority = {'SERVICE-11': 40, 'DNS-YT-04': 39} if messenger_issue(query) or messenger_issue(context) else {}
    scored = []
    for article in rows:
        title, body = article['intent'].casefold(), article['step'].casefold()
        primary = sum(8 if word in title else 2 if word in body else 0 for word in tokens)
        related = sum(3 if word in title else 1 if word in body else 0 for word in context_tokens)
        score = primary + related + priority.get(article['id'], 0)
        if score:
            scored.append((score, article['id'], article))
    return [article for _, _, article in sorted(scored, key=lambda item: (-item[0], item[1]))[:limit]]
