"""Telegram support material -> small, typed web keyboard; never return raw HTML."""
import json,re
from html.parser import HTMLParser
from urllib.parse import urlsplit,parse_qs


def safe_url(value):
    if not isinstance(value,str) or len(value)>2048:return None
    from app.services.freeman_ai.v21.registry import URLS, is_cabinet_key_url
    from app.services.freeman_ai.v21.support_content import APPLE_REGION
    if value in URLS.values() or is_cabinet_key_url(value) or value==APPLE_REGION:return value
    if re.fullmatch(r'https://freemanone\.pro/support/(action|connect|renew)#[A-Za-z0-9_-]{32}',value):return value
    try:p=urlsplit(value)
    except ValueError:return None
    if p.username or p.password or p.fragment or any(ord(c)<32 for c in value):return None
    if not p.scheme and not p.netloc and value.startswith('/') and not value.startswith('//'):
        return value if p.path in ('/buy','/buy/renew') else None
    if p.scheme!='https' or p.netloc!=p.hostname:return None
    if value in ('https://yandex.ru/internet/','https://t.me/FreemanOneBot','https://www.happ.su/main/ru'):return value
    if p.hostname=='freemanone.pro' and p.path in ('/buy','/buy/renew'):return value
    if p.hostname=='apps.apple.com' and re.fullmatch(r'/kz/(?:[^/?#]+/)*id[0-9]+/?',p.path):return value
    if p.hostname=='play.google.com' and p.path=='/store/apps/details' and re.fullmatch(r'[A-Za-z0-9_.]{3,200}',parse_qs(p.query).get('id',[''])[0]):return value
    return None


class Keyboard(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.items=[];self.row=-1;self.button=None
    def handle_starttag(self,tag,attrs):
        if tag=='tg-button-row':self.row+=1
        if tag=='tg-button':self.button=[dict(attrs),[]]
    def handle_data(self,data):
        if self.button:self.button[1].append(data)
    def handle_endtag(self,tag):
        if tag!='tg-button' or not self.button:return
        attrs,parts=self.button;self.button=None
        label=''.join(parts).strip()[:120];kind=attrs.get('type');item=None
        if kind=='url':item=dict(type='link',label=label,url=attrs.get('url'))
        elif kind=='copy_text':item=dict(type='copy',label=label,text=attrs.get('text',''))
        elif kind=='callback_data' and re.fullmatch(r'fo:(region|install):[1-9][0-9]{0,18}',attrs.get('data','')):item=dict(type='material',label=label,material='key-install' if attrs['data'].startswith('fo:install:') else 'appstore-region')
        if item:self.items.append(dict(item,row=max(0,self.row)))


def web_actions(text, actions):
    if isinstance(actions,str):
        try:actions=json.loads(actions)
        except (TypeError,ValueError):actions=[]
    if not isinstance(actions,list):actions=[]
    candidates=[]
    for action in actions[:80]:
        if not isinstance(action,dict):continue
        if action.get('kind')=='rich_material' and isinstance(action.get('html'),str):
            parser=Keyboard()
            try:parser.feed(action['html'][:50000]);candidates.extend(parser.items)
            except (ValueError,TypeError):pass
        else:candidates.append(action)
    # Old persisted answers may contain only an official store link in their text.
    for url in re.findall(r'https://(?:apps\.apple\.com|play\.google\.com)/[^\s<>"\]]+',str(text or '')[:20000]):
        url=url.rstrip('.,;)')
        candidates.append(dict(type='link',url=url,label='Открыть в App Store' if 'apps.apple.com' in url else 'Открыть в Google Play'))
    result=[];seen=set()
    for action in candidates:
        label=str(action.get('label') or '')[:120].strip();kind=action.get('type');item=None
        if not label:continue
        if kind=='link' and safe_url(action.get('url')):item=dict(type='link',label=label,url=action['url'])
        elif kind in ('copy','copy_text') and isinstance(action.get('text'),str) and 0<len(action['text'])<=512:item=dict(type='copy',label=label,text=action['text'])
        elif kind=='agent_action' and re.fullmatch(r'[A-Za-z0-9_-]{32}',str(action.get('token',''))) and action.get('decision','open') in ('open','confirm','cancel'):item=dict(type=kind,label=label,token=action['token'],decision=action.get('decision','open'))
        elif kind=='agent_image' and re.fullmatch(r'[A-Za-z0-9_-]{32}',str(action.get('token',''))):item=dict(type=kind,label=label,token=action['token'])
        elif kind=='material' and action.get('material') in ('appstore-region','key-install'):item=dict(type='material',label=label,material=action['material'])
        if not item:continue
        identity=(item['type'],item.get('url'),item.get('text'),item.get('material'),item.get('token'),item.get('decision'))
        if identity in seen:continue
        seen.add(identity);row=action.get('row',len(result));item['row']=max(0,min(23,row)) if type(row) is int else len(result);result.append(item)
        if len(result)>=24:break
    if result:
        return result

    from app.services.freeman_ai.v21.support_content import SCOPE_OPTIONS, SCOPE_QUESTION
    template = SCOPE_QUESTION + '\n\n' + '\n\n'.join(
        f'{number}. {label}' for number, label in enumerate(SCOPE_OPTIONS, 1)
    )
    if str(text or '').strip() == template:
        return [
            dict(type='reply_choice', label=label, row=row)
            for row, label in enumerate(SCOPE_OPTIONS)
        ]
    return []
