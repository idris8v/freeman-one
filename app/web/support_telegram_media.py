"""One authorized rich-media reply per operator message; no website redirect."""
import re
from html import escape,unescape
from pathlib import Path
from urllib.parse import urlsplit
from sqlalchemy import select
from aiogram.types import InputRichMessage,InputRichMessageMedia,InputMediaPhoto,InputMediaVideo,FSInputFile
from app.database.models import Ticket,TicketMessage,User,WebSupportAttachment as A
from app.services.support_alerts import formatted_excerpt
KIND='support_media_reply'

def body_html(text):
    def copy_button(value):
        return '<tg-button type="copy_text" text="'+escape(value,quote=True)+'">'+escape(value)+'</tg-button>'
    lines=[]
    for line in text.splitlines():
        field=re.match(r'^(.*?(?:Улица|Город|Регион|Индекс|Телефон):)(?:\*\*|__)?\s*(.+)$',line)
        if field:
            label=field[1].replace('*','').replace('_','')
            value=field[2].strip()
            if len(value)>1 and value.startswith('`') and value.endswith('`'):value=value[1:-1]
            value=re.sub(r'^(?:\*\*|__)(.*)(?:\*\*|__)$',r'\1',value)
            lines.append(escape(label)+' '+copy_button(value) if len(value)<=512 else formatted_excerpt(line,limit=20000))
        else:
            rendered=formatted_excerpt(line,limit=20000)
            rendered=re.sub(r'<code>(.*?)</code>',lambda m:copy_button(unescape(m[1])) if len(unescape(m[1]))<=512 else m[0],rendered)
            lines.append(rendered)
    body='<br>'.join(lines)
    links=[];seen=set()
    labelled={url:label for label,url in re.findall(r'\[([^\]\n]+)\]\((https://[^\s)]+)\)',text)}
    for raw in re.findall(r'https://[^\s<>\])]+',text):
        url=raw.rstrip('.,;');p=urlsplit(url)
        if not p.hostname or p.username or p.password or any(ord(c)<32 for c in url) or url in seen:continue
        seen.add(url);label=labelled.get(url,p.hostname)
        links.append('<tg-button-row><tg-button type="url" url="'+escape(url,quote=True)+'">'+escape(label[:100])+'</tg-button></tg-button-row>')
        if len(links)>=12:break
    return body+''.join(links)

async def prepare(session,job,root):
    if job.target_id<=0 or not job.dedup.startswith('ticket.message:'):return None
    try:mid=int(job.dedup.split(':')[1])
    except (ValueError,IndexError):return None
    ticket=await session.get(Ticket,job.ticket_id);customer=await session.get(User,ticket.user_id) if ticket else None
    message=await session.get(TicketMessage,mid)
    if (not customer or customer.telegram_id!=job.target_id or customer.status!='active'
        or not message or message.ticket_id!=ticket.id or message.role!='admin' or not message.source.startswith('web:')):return None
    assets=list(await session.scalars(select(A).where(A.message_id==mid).order_by(A.id)))
    if not assets or len(assets)>12:return None
    media=[];html=[]
    for a in assets:
        if a.kind not in ('image','video') or Path(a.storage_name).name!=a.storage_name:return None
        path=root/a.storage_name
        if path.is_symlink() or not path.is_file():raise ValueError('support_media_missing')
        ident='asset'+str(a.id);kind='photo' if a.kind=='image' else 'video'
        cls=InputMediaPhoto if kind=='photo' else InputMediaVideo
        media.append(InputRichMessageMedia(id=ident,media=cls(media=FSInputFile(path,filename=a.original_name))))
        html.append('<img src="tg://photo?id='+ident+'">' if kind=='photo' else '<video src="tg://video?id='+ident+'"></video>')
    html.append(body_html(message.text))
    return InputRichMessage(html=''.join(html),media=media,skip_entity_detection=True)
