# -*- coding: utf-8 -*-
"""Approved support materials rendered outside secret-redaction of model text."""
import html,json,re
import aiohttp
from urllib.parse import urlsplit
WINDOWS_HAPP_URL = 'https://www.happ.su/main/ru'
MATERIALS_WITH_INTRO = ('key_install', 'downloads', 'region_check', 'desktop_download')


TOOL=dict(type='function',name='support_material',description='Send verified app download buttons, requested media or diagnostic materials. This tool is not a substitute for conversation or step-by-step installation coaching. Never invent store links.',strict=True,parameters=dict(type='object',properties={'explanation':{'type':'string','maxLength':2000,'description':'Write the actual client-facing reply in your own words for this conversation, with paragraphs and natural warmth. For downloads, explain the next step and ask the client to reply after installing. For ping/speed, explain the relevant check using known context. Never paste a stock guide or claim unverified applications are available. Let the verified buttons show the available apps. Verified buttons and media are attached by the server; do not print URLs or fake buttons. For region_guide and key_transfer the server will obtain facts and then ask you to compose the final reply.'},'material':{'type':'string','enum':['region_guide','downloads','region_check','key_transfer','key_install','payment_help','speed_test','ping_guide','desktop_download']},'platform':{'type':'string','enum':['ios','android','windows','macos']},'region':{'type':'string','enum':['kz']},'app':{'type':'string','enum':['all','Happ','INCY','v2RayTun']}},required=['explanation','material','platform','region','app'],additionalProperties=False))
IOS={'Happ':6504287215,'INCY':6756943388,'v2RayTun':6476628951}
ANDROID={'v2RayTun':'https://play.google.com/store/apps/details?id=com.v2raytun.android','INCY':'https://play.google.com/store/apps/details?id=llc.itdev.incy'}
def guide_html(text,fields,video):
 out=[]
 for i,line in enumerate(text.splitlines()):
  rendered=html.escape(line)
  rendered=re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>",rendered)
  if i==0 and "<b>" not in rendered and (video or fields):rendered="<b>"+rendered+"</b>"
  for value in set(fields.values()):
   if value and value in line:
    safe=html.escape(value,quote=True);rendered=rendered.replace(html.escape(value),'<tg-button type="copy_text" text="'+safe+'">'+safe+'</tg-button>')
  spacer='<p>&nbsp;</p>'
  if line and re.match(r'^(?:[1-9]\ufe0f?\u20e3|[0-9]+[.)])',line) and out and out[-1]!=spacer:out.append(spacer)
  if line:out.append('<p>'+rendered+'</p>')
  elif out and out[-1]!=spacer:out.append(spacer)
  if i==0 and video:out.append('<video src="tg://video?id=region_guide"/>')
 return '\n'.join(out)
def material_with_intro(text, markup, intro):
    return f'{intro}\n\n{text}', f'{guide_html(intro, {}, False)}\n<p>&nbsp;</p>\n{markup}'

def region_question_html(mid):
 if not isinstance(mid,int) or mid<=0:raise ValueError('Invalid message identifier')
 return '<p>Подскажите, в App Store сейчас выбран Казахстан?</p><p>&nbsp;</p><p>Если регион ещё не меняли, нажмите кнопку — пришлю видео и пошаговую инструкцию.</p><p>&nbsp;</p><tg-button-row><tg-button type="callback_data" data="fo:region:'+str(mid)+'">Как сменить регион</tg-button></tg-button-row>'
def downloads_html(platform,region,found,missing):
 if platform not in ('ios','android') or region!='kz':raise ValueError('Unsupported store context')
 out=['<p>Выберите приложение для установки'+(' — App Store Казахстана' if platform=='ios' else ' на Android')+':</p>']
 for item in found:
  p=urlsplit(item['url'])
  if p.scheme!='https' or p.username or p.password or (platform=='ios' and (p.netloc!='apps.apple.com' or not p.path.startswith('/kz/'))) or (platform=='android' and p.netloc!='play.google.com'):raise ValueError('Untrusted store URL')
  out.append('<tg-button-row><tg-button type="url" url="'+html.escape(item['url'],quote=True)+'">Скачать '+html.escape(item['name'])+'</tg-button></tg-button-row>')
 if missing:out.append('<p>Не удалось подтвердить доступность: '+html.escape(', '.join(missing))+'. Можно выбрать приложение из кнопок выше.</p>')
 return '\n'.join(out)
async def store_apps(platform,region,app,client):
 if platform not in ('ios','android') or region!='kz' or app not in ('all',*IOS):raise ValueError('Unsupported catalogue request')
 names=list(IOS) if app=='all' else [app];found=[];missing=[]
 for name in names:
  if platform=='android':
   if name in ANDROID:found.append(dict(name=name,url=ANDROID[name]))
   else:missing.append(name)
   continue
  try:
   async with client.get('https://itunes.apple.com/lookup',params={'id':IOS[name],'country':region},allow_redirects=False) as response:
    if response.status!=200:raise ValueError('Store unavailable')
    data=await response.json(content_type=None)
   rows=[x for x in data.get('results',[]) if x.get('trackId')==IOS[name]]
   if not rows:missing.append(name);continue
   url=rows[0].get('trackViewUrl','');downloads_html(platform,region,[dict(name=name,url=url)],[]);found.append(dict(name=name,url=url))
  except (ValueError,TimeoutError,aiohttp.ClientError):missing.append(name)
 return downloads_html(platform,region,found,missing)

def payment_help_html(text):
    links = (
        ('https://t.me/FreemanOneBot', 'Открыть новый бот'),
        ('https://freemanone.pro/buy', 'Купить без регистрации'),
    )
    return guide_html(text, {}, False) + ''.join(
        f'<tg-button-row><tg-button type="url" url="{url}">{label}</tg-button></tg-button-row>'
        for url, label in links
    )


def authored_material_html(reply, markup, mid):
    """Keep approved UI/media; never inherit a stock material's paragraphs."""
    from html.parser import HTMLParser
    from app.web.support_message_actions import web_actions

    if type(mid) is not int or mid <= 0:
        raise ValueError('Invalid message identifier')

    class Media(HTMLParser):
        def __init__(self):
            super().__init__();self.items=[]

        def handle_starttag(self, tag, attrs):
            source=dict(attrs).get('src')
            if (tag,source) == ('img','tg://photo?id=ping_guide'):
                self.items.append('<img src="tg://photo?id=ping_guide"/>')
            elif (tag,source) == ('video','tg://video?id=region_guide'):
                self.items.append('<video src="tg://video?id=region_guide"/>')

    parser=Media();parser.feed(markup)
    buttons=web_actions('',[dict(kind='rich_material',html=markup)])
    fields={str(index):button['text'] for index,button in enumerate(buttons) if button['type']=='copy'}
    result=''.join(dict.fromkeys(parser.items))+guide_html(reply,fields,False)
    for button in buttons:
        kind=button['type'];label=html.escape(button['label'])
        if kind=='copy' and button['text'] in reply:
            continue  # The value is already a copy button inside the authored text.
        if kind=='link':
            attrs='type="url" url="'+html.escape(button['url'],quote=True)+'"'
        elif kind=='copy':
            attrs='type="copy_text" text="'+html.escape(button['text'],quote=True)+'"'
        else:
            material='install' if button['material']=='key-install' else 'region'
            attrs='type="callback_data" data="fo:'+material+':'+str(mid)+'"'
        result+='<tg-button-row><tg-button '+attrs+'>'+label+'</tg-button></tg-button-row>'
    return result
