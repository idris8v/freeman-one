"""One safe presentation contract for web and Telegram Rich messages."""
import re
from html import escape
from html.parser import HTMLParser
from .registry import URLS, is_cabinet_key_url, key_link
from .support_content import APPLE_REGION

def copy_action(value,fallback_url):
    if not isinstance(value,str) or not value:raise ValueError('empty_copy_value')
    if len(value)<=256:return dict(type='copy',label='Скопировать ключ',text=value)
    return dict(type='link',label='Открыть и скопировать ключ',url=fallback_url)

def html_reply(text,actions):
    body=escape(text)
    body=re.sub(r'\*\*([^*\n]+)\*\*',r'<b>\1</b>',body)
    body=re.sub(r'`([^`\n]+)`',r'<code>\1</code>',body)
    body=body.replace('\n','<br>')
    buttons=[]
    for a in actions:
        label=escape(str(a.get('label',''))[:120])
        if not label:continue
        kind=a.get('type');button=None
        if kind=='link':
            u=a.get('url','')
            # Only server-owned link registry or opaque same-site pages. No
            # custom URL from a model/tool output goes into a keyboard.
            if u in URLS.values() or u==APPLE_REGION or re.fullmatch(r'https://apps\.apple\.com/kz/app/[a-z0-9-]+/id[0-9]+',u) or is_cabinet_key_url(u) or re.fullmatch(r'https://freemanone\.pro/support/(?:action|connect|renew)#[A-Za-z0-9_-]{32}',u):
                button=f'<tg-button type="url" url="{escape(u,quote=True)}">{label}</tg-button>'
        elif kind=='copy' and isinstance(a.get('text'),str) and 1<=len(a['text'])<=256:
            button=f'<tg-button type="copy_text" text="{escape(a["text"],quote=True)}">{label}</tg-button>'
        elif kind=='agent_action' and re.fullmatch(r'[A-Za-z0-9_-]{32}',a.get('token','')):
            decision={'open':'o','confirm':'y','cancel':'n'}.get(a.get('decision','open'))
            if decision:button=f'<tg-button type="callback_data" data="f21:{a["token"]}:{decision}">{label}</tg-button>'
        if button:buttons.append('<tg-button-row>'+button+'</tg-button-row>')
    return body+''.join(buttons)

def fallback_keyboard(actions):
    """Build native buttons from the same validated representation as rich text."""
    class Buttons(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows=[]
            self.button=None

        def handle_starttag(self,tag,attrs):
            if tag!='tg-button':
                return
            fields=dict(attrs)
            kind=fields['type']
            value=fields['text'] if kind=='copy_text' else fields['data'] if kind=='callback_data' else fields['url']
            self.button={'text':'',kind:{'text':value} if kind=='copy_text' else value}

        def handle_data(self,data):
            if self.button is not None:
                self.button['text']+=data

        def handle_endtag(self,tag):
            if tag=='tg-button' and self.button is not None:
                self.rows.append([self.button])
                self.button=None

    parser=Buttons()
    parser.feed(html_reply('',actions))
    return {'inline_keyboard':parser.rows}

def verified_qr(value):
    """Generate and independently decode every QR before exposing it."""
    import qrcode
    import zxingcpp
    from io import BytesIO
    if not key_link(value):raise ValueError('unsupported_qr_value')
    qr=qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,border=4,box_size=8)
    qr.add_data(value);qr.make(fit=True)
    image=qr.make_image(fill_color='black',back_color='white').convert('RGB')
    decoded=zxingcpp.read_barcode(image)
    if not decoded or decoded.text!=value:raise ValueError('qr_roundtrip_failed')
    data=BytesIO();image.save(data,format='PNG');return data.getvalue()
