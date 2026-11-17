"""Publications CMS. Reuses WebNews identity and durable DispatchWorker outbox."""
from app.web.access import require_business_admin
import asyncio,base64,binascii,html,json,re,secrets,logging
from pathlib import Path
from datetime import datetime,timezone
from html.parser import HTMLParser
from fastapi import APIRouter,Cookie,HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel,Field
from sqlalchemy.orm import defer
from sqlalchemy import select,func,or_,delete,update,insert,literal,cast,String
from app.database.models import User,WebNews,WebUsefulLink,Campaign,Delivery,AdminAudit,Publication,PublicationView,PublicationMedia,now
from app.database.repositories import SQLRepository
from app.web.publication_content import render_content,MEDIA_RE
from app.services.publication_urls import publication_url,legacy_section_target
STORAGE=Path('/app/data/web-posts')
STATES={'draft','published','scheduled','archived','deleted'}
async def store_json(repo,key,value):
    raw=json.dumps(value,ensure_ascii=False)
    parts=[raw[i:i+3800] for i in range(0,len(raw),3800)]
    if len(parts)==1:await repo.set_setting(key,raw)
    else:
        for i,part in enumerate(parts):await repo.set_setting(key+':chunk:'+str(i),part)
        await repo.set_setting(key,json.dumps({'_chunks':len(parts)}))
async def load_json(repo,key):
    raw=await repo.setting(key)
    if not raw:return None
    data=json.loads(raw)
    if '_chunks' in data:data=json.loads(''.join([await repo.setting(key+':chunk:'+str(i)) for i in range(data['_chunks'])]))
    return data
class MediaItem(BaseModel):
    name:str=Field(pattern='^'+MEDIA_RE+'$')
    kind:str=Field(pattern='^(image|video)$')
    alt:str=Field(default='',max_length=200)
    caption:str=Field(default='',max_length=500)
    inline:bool=False
    cover_only:bool=False
class PostPayload(BaseModel):
    title:str=Field(default='',max_length=160)
    section:str=Field(default='news',pattern='^(news|links|faq|instructions)$')
    help_key:str=Field(default='',max_length=80)
    content:dict=Field(default_factory=lambda:{'ops':[{'insert':'\n'}]})
    media:list[MediaItem]=Field(default_factory=list,max_length=10)
    cover:str=Field(default='',max_length=64)
    slideshow:bool=False
    pinned:bool=False
    notify_clients:bool=True
    scheduled_at:datetime|None=None
    version:int|None=None
class ActionPayload(BaseModel):
    action:str=Field(pattern='^(publish|archive|delete|restore|duplicate|purge|retry_notification)$')
    version:int
class MediaPayload(BaseModel):
    name:str=Field(max_length=255)
    mime:str=Field(max_length=80)
    data:str=Field(max_length=28000000)

class LegacyDelta(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.ops=[];self.stack=[]
    def attrs(self):
        result={}
        for _,attrs in self.stack:result.update(attrs)
        return result
    def handle_starttag(self,tag,attrs):
        a=dict(attrs);f={'b':{'bold':True},'strong':{'bold':True},'i':{'italic':True},'em':{'italic':True},'u':{'underline':True},'s':{'strike':True},'blockquote':{'blockquote':True},'pre':{'code-block':True}}
        if tag=='a' and a.get('href','').startswith('https://'):f['a']={'link':a['href']}
        if tag=='br':self.ops.append({'insert':'\n'});return
        self.stack.append((tag,f.get(tag,{})))
    def handle_endtag(self,tag):
        if tag in ('p','div','li','blockquote','pre'):self.ops.append({'insert':'\n','attributes':self.attrs()})
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:self.stack=self.stack[:i];break
    def handle_data(self,data):self.ops.append({'insert':data,'attributes':self.attrs()})
async def migrate_publications(database,settings):
    async with database.sessions.begin() as s:
        repo=SQLRepository(s)
        rows=list(await s.scalars(select(WebNews).outerjoin(Publication,Publication.id==WebNews.id).where(Publication.id.is_(None))))
        for n in rows:
            old=await load_json(repo,'web_post:'+str(n.id))
            if not old:old=dict(body=html.escape(n.text),category='news',media=[],legacy=True)
            media=[dict(name=name,kind='video' if name.endswith(('.mp4','.webm','.mov')) else 'image',alt='',caption='',inline=False) for name in old.get('media',[]) if re.fullmatch(MEDIA_RE,name)]
            parser=LegacyDelta();parser.feed(old.get('body',''))
            from app.web.publication_content import valid_url
            legacy_target=legacy_section_target(settings,n.target)
            if old.get('legacy') and valid_url(legacy_target):parser.ops += [{'insert':{'button':dict(text='Открыть',url=legacy_target)}},{'insert':'\n'}]
            content,body,plain=render_content({'ops':parser.ops},media)
            section='news' if old.get('category')=='news' else 'links';n.category=section
            p=Publication(id=n.id,section=section,status='published' if n.published else 'draft',content=json.dumps(content,ensure_ascii=False),body=body,plain=plain,media=json.dumps(media),cover=next((m['name'] for m in media if m['kind']=='image'),''),slideshow=old.get('slideshow',False),notify_clients=not old.get('legacy',False),pinned=False,published_at=n.created_at if n.published else None,updated_at=n.created_at,campaign_id=old.get('campaign_id'),link_id=old.get('link_id'),notified_at=n.created_at if old.get('campaign_id') else None,version=1)
            s.add(p);await s.flush();await sync_visibility(s,p,n,settings)
        # Existing useful links become published, editable articles, retaining the link identity.
        from app.web.publication_content import valid_url
        link_ids=select(Publication.link_id).where(Publication.link_id.is_not(None))
        for link in await s.scalars(select(WebUsefulLink).where(WebUsefulLink.id.not_in(link_ids))):
            if await repo.setting(f'legacy_link_migrated:{link.id}')=='1':continue
            target=legacy_section_target(settings,link.target)
            ops=[{'insert':link.label+'\n'}]
            if valid_url(target):ops += [{'insert':{'button':dict(text='Открыть',url=target)}},{'insert':'\n'}]
            content,body,plain=render_content({'ops':ops},[])
            n=WebNews(title=link.label,category='links',published=False,created_at=link.created_at);s.add(n);await s.flush()
            p=Publication(id=n.id,section='links',status='published' if link.enabled else 'archived',content=json.dumps(content,ensure_ascii=False),body=body,plain=plain,media='[]',link_id=link.id,notify_clients=False,published_at=link.created_at if link.enabled else None,updated_at=now(),version=1);s.add(p);await s.flush();await sync_visibility(s,p,n,settings)
            await repo.set_setting(f'legacy_link_migrated:{link.id}','1')
        await repo.set_setting('legacy_publication_migrated','1')
        await repo.set_setting('publications_schema_version','193')
async def sync_visibility(s,p,n,settings):
    n.published=p.status=='published';n.category=p.section;n.text=p.plain[:500];n.target=publication_url(settings,n.id)
    from app.web.cabinet.help_content import replace_legacy_on_publish
    await replace_legacy_on_publish(s,p)
    if p.section=='links' and p.status=='published' and not p.link_id:
        link=WebUsefulLink(label=n.title[:100],target=n.target,icon='file-text',position=0 if p.pinned else 100,enabled=True);s.add(link);await s.flush();p.link_id=link.id
    if p.link_id:
        link=await s.get(WebUsefulLink,p.link_id)
        if link:
            link.target=n.target;link.enabled=p.section=='links' and p.status=='published';link.label=n.title[:100];link.position=0 if p.pinned else 100
            await SQLRepository(s).set_setting(f'legacy_link_migrated:{link.id}','1')
async def cancel_delivery(s,p):
    if p.campaign_id:
        campaign=await s.get(Campaign,p.campaign_id)
        if campaign:campaign.status='cancelled'
        await s.execute(update(Delivery).where(Delivery.campaign_id==p.campaign_id,Delivery.status.in_(('pending','sending'))).values(status='skipped'))
async def publish_record(s,p,n,settings):
    if not n.title.strip() or not (p.plain.strip() or json.loads(p.media)):raise HTTPException(422,'Для публикации нужны заголовок и текст или медиа')
    p.status='published';p.scheduled_at=None;p.published_at=now();p.updated_at=now();p.version+=1
    await sync_visibility(s,p,n,settings)
    if p.section in ('faq','instructions'):p.notify_clients=False
    if p.notify_clients and p.notified_at is None:
        # Content is committed together with this outbox; edits cannot enqueue it twice.
        from app.web.admin_digest import enqueue_publication_push
        await enqueue_publication_push(s,p,n.title)
        text='Новая публикация\n'+n.title+'\n\n'+p.plain[:600]
        campaign=Campaign(actor_id=(await s.get(User,p.updated_by)).telegram_id if p.updated_by else 0,audience='all',text=text,photo='post:'+str(p.id),status='queued');s.add(campaign);await s.flush()
        p.campaign_id=campaign.id;p.notified_at=now()
        snapshot=dict(id=p.id,title=n.title,body=html.escape(p.plain[:600]),media=[m['name'] for m in json.loads(p.media)])
        await store_json(SQLRepository(s),'web_post_delivery:'+str(campaign.id),snapshot)
        await s.execute(insert(Delivery).from_select(['dedup','user_id','campaign_id','text','photo','status','attempts','available_at','created_at'],select(literal('webpost:'+str(p.id)+':')+cast(User.id,String),User.id,literal(campaign.id),literal(text),literal('post:'+str(p.id)),literal('pending'),literal(0),literal(now()),literal(now())).where(User.telegram_id.is_not(None),User.status!='blocked')))
async def publication_tick(database,settings):
    async with database.sessions.begin() as s:
        rows=list(await s.scalars(select(Publication).where(Publication.status=='scheduled',Publication.scheduled_at<=now()).with_for_update(skip_locked=True).limit(25)))
        for p in rows:
            n=await s.get(WebNews,p.id);await publish_record(s,p,n,settings)
            await SQLRepository(s).add(AdminAudit(actor_telegram_id=0,action='publication_scheduled_publish',target=str(p.id),details='scheduled'))
async def publication_worker(database,settings):
    while True:
        try:await publication_tick(database,settings)
        except asyncio.CancelledError:raise
        except Exception as e:logging.getLogger(__name__).warning('Publication scheduler: %s',type(e).__name__)
        await asyncio.sleep(15)

def posts_router(database,settings,current_user):
    router=APIRouter()
    async def admin(token):
        actor=await current_user(token)
        require_business_admin(actor,settings)
        return actor
    def metadata(p,n,views=0):
        media=json.loads(p.media);cover=p.cover or next((m['name'] for m in media if m['kind']=='image' and not m.get('inline')),'')
        date=p.scheduled_at if p.status=='scheduled' else p.published_at if p.status=='published' else p.deleted_at if p.status=='deleted' else p.updated_at
        return dict(id=p.id,url=publication_url(settings,p.id),title=n.title,excerpt=p.plain[:160],plain=p.plain[:500],section=p.section,category=p.section,status=p.status,published=p.status=='published',views=views,pinned=p.pinned,notify=p.notified_at is not None and p.notify_clients,notify_clients=p.notify_clients,created_at=n.created_at.isoformat(),updated_at=p.updated_at.isoformat(),published_at=p.published_at.isoformat() if p.published_at else None,scheduled_at=p.scheduled_at.isoformat() if p.scheduled_at else None,date=date.isoformat(),thumbnail='/api/web/posts/media/'+cover+'?thumb=true' if cover else '',version=p.version)
    async def full(s,p,n):
        views=await s.scalar(select(func.count()).select_from(PublicationView).where(PublicationView.publication_id==p.id));result=metadata(p,n,views)
        content=json.loads(p.content);body=p.body;plain=p.plain
        if p.created_by is None:
            # Old migrated section buttons are derived navigation. Re-render them
            # for this deployment without rewriting the stored document/version.
            changed=False
            for op in content['ops']:
                value=op.get('insert')
                button=value.get('button') if isinstance(value,dict) else None
                if button:
                    target=legacy_section_target(settings,button['url'])
                    if target!=button['url']:button['url']=target;changed=True
            if changed:content,body,plain=render_content(content,json.loads(p.media))
        result.update(content=content,body=body,plain=plain,media=json.loads(p.media),cover=p.cover,slideshow=p.slideshow,help_key=await SQLRepository(s).setting('support_help_origin:'+str(p.id)))
        if p.campaign_id:result['delivery']=dict((await s.execute(select(Delivery.status,func.count()).where(Delivery.campaign_id==p.campaign_id).group_by(Delivery.status))).all())
        if p.notified_at:
            from app.web.admin_digest import publication_push_status
            result['push_delivery']=await publication_push_status(s,p.id)
        return result
    @router.get('/api/web/admin/publications')
    async def listing(q:str='',status:str='all',section:str='all',media:str='all',notification:str='all',pinned:str='all',period:str='all',start:str='',end:str='',page:int=1,limit:int=10,freeman_session:str|None=Cookie(default=None)):
        await admin(freeman_session)
        if page<1 or limit not in (10,25,50) or len(q)>200 or status not in STATES|{'all'} or section not in ('all','news','links','faq','instructions','help') or media not in ('all','none','images','slideshow','video') or notification not in ('all','yes','no') or pinned not in ('all','yes','no'):raise HTTPException(422,'Неверный фильтр')
        from app.web.admin_reports import period_bounds
        conditions=[]
        if status!='all':conditions.append(Publication.status==status)
        if section=='help':conditions.append(Publication.section.in_(('faq','instructions')))
        elif section!='all':conditions.append(Publication.section==section)
        if q.strip():
            escaped=q.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_');pattern='%'+escaped+'%'
            author=select(User.id).where(or_(User.first_name.ilike(pattern,escape='\\'),User.username.ilike(pattern,escape='\\')))
            conditions.append(or_(WebNews.title.ilike(pattern,escape='\\'),Publication.plain.ilike(pattern,escape='\\'),Publication.content.ilike(pattern,escape='\\'),cast(Publication.id,String).ilike(pattern,escape='\\'),Publication.created_by.in_(author),Publication.section=='news' if q.lower()=='новости' else Publication.section=='links' if q.lower() in ('полезные ссылки','инструкции') else Publication.section.ilike(pattern,escape='\\')))
        if pinned!='all':conditions.append(Publication.pinned==(pinned=='yes'))
        if notification!='all':conditions.append(Publication.notified_at.is_not(None) if notification=='yes' else Publication.notified_at.is_(None))
        if media=='none':conditions.append(Publication.media=='[]')
        if media=='slideshow':conditions.append(Publication.slideshow==True)
        if media in ('images','video'):conditions.append(Publication.media.like('%"kind": "'+('image' if media=='images' else 'video')+'"%'))
        if period!='all':
            lo,hi=period_bounds(period,start,end);date=func.coalesce(Publication.scheduled_at,Publication.published_at,Publication.updated_at);conditions.extend([date>=lo,date<hi])
        async with database.sessions() as s:
            base=select(Publication,WebNews).options(defer(Publication.content),defer(Publication.body)).join(WebNews,WebNews.id==Publication.id).where(*conditions)
            total=await s.scalar(select(func.count()).select_from(base.subquery()));page=min(page,max(1,(total+limit-1)//limit))
            counts=dict((await s.execute(select(Publication.status,func.count()).group_by(Publication.status))).all());counts['all']=sum(counts.values())
            views=select(PublicationView.publication_id,func.count().label('views')).group_by(PublicationView.publication_id).subquery()
            order=Publication.scheduled_at.asc() if status=='scheduled' else Publication.deleted_at.desc() if status=='deleted' else Publication.published_at.desc() if status=='published' else Publication.updated_at.desc()
            rows=(await s.execute(base.add_columns(func.coalesce(views.c.views,0)).outerjoin(views,views.c.publication_id==Publication.id).order_by(Publication.pinned.desc(),order,Publication.id.desc()).limit(limit).offset((page-1)*limit))).all()
            return dict(items=[metadata(p,n,v) for p,n,v in rows],total=total,page=page,limit=limit,counts=counts)
    @router.get('/api/web/posts')
    async def client_listing(category:str='',admin_view:bool=False,admin:bool=False,page:int=1,limit:int=50,freeman_session:str|None=Cookie(default=None)):
        actor=await current_user(freeman_session)
        if admin or admin_view:
            require_business_admin(actor,settings)
        if page<1 or not 1<=limit<=50:raise HTTPException(422,'Неверная страница')
        async with database.sessions() as s:
            query=select(Publication,WebNews).options(defer(Publication.content),defer(Publication.body)).join(WebNews,WebNews.id==Publication.id).where(True if admin or admin_view else Publication.status=='published')
            if category=='instructions':query=query.where(Publication.section.in_(('links','instructions')))
            elif category:query=query.where(Publication.section==category)
            rows=(await s.execute(query.order_by(Publication.pinned.desc(),Publication.published_at.desc(),Publication.id.desc()).limit(limit).offset((page-1)*limit))).all()
            return [metadata(p,n) for p,n in rows]
    @router.get('/api/web/posts/{id}')
    async def detail(id:int,freeman_session:str|None=Cookie(default=None)):
        actor=await current_user(freeman_session)
        async with database.sessions() as s:
            p=await s.get(Publication,id);n=await s.get(WebNews,id)
            if not p or (p.status!='published' and not settings.is_owner(actor.telegram_id)):raise HTTPException(404,'Публикация не найдена')
            result=await full(s,p,n)
            if not settings.is_owner(actor.telegram_id):result.pop('delivery',None);result.pop('push_delivery',None)
            return result
    @router.post('/api/web/posts/{id}/view')
    async def view(id:int,freeman_session:str|None=Cookie(default=None)):
        actor=await current_user(freeman_session)
        async with database.sessions.begin() as s:
            p=await s.get(Publication,id)
            if not p or p.status!='published':raise HTTPException(404,'Публикация не найдена')
            if not settings.is_owner(actor.telegram_id):
                # PostgreSQL unique constraint enforces one authenticated view per user.
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from sqlalchemy.dialects.sqlite import insert as sqlite_insert
                stmt=(pg_insert if s.bind.dialect.name=='postgresql' else sqlite_insert)(PublicationView).values(publication_id=id,user_id=actor.id,created_at=now()).on_conflict_do_nothing(index_elements=['publication_id','user_id'])
                await s.execute(stmt)
            return {'ok':True}
    @router.post('/api/web/admin/publications/preview')
    async def preview(payload:PostPayload,freeman_session:str|None=Cookie(default=None)):
        await admin(freeman_session)
        media=[m.model_dump() for m in payload.media]
        if any(not (STORAGE/m['name']).is_file() for m in media):raise HTTPException(422,'Медиа не найдено')
        document,body,plain=render_content(payload.content,media)
        return {'content':document,'body':body,'plain':plain}
    @router.post('/api/web/admin/publications')
    @router.put('/api/web/admin/publications/{id}')
    async def save(payload:PostPayload,id:int|None=None,freeman_session:str|None=Cookie(default=None)):
        actor=await admin(freeman_session);media=[m.model_dump() for m in payload.media]
        if len({m['name'] for m in media})!=len(media):raise HTTPException(422,'Медиа не должны повторяться')
        if payload.cover and not any(m['name']==payload.cover and m['kind']=='image' for m in media):raise HTTPException(422,'Обложка должна быть изображением публикации')
        if payload.slideshow and len([m for m in media if m['kind']=='image' and not m['inline'] and not m.get('cover_only')])<2:raise HTTPException(422,'Для слайд-шоу нужны минимум два изображения')
        for m in media:
            expected='video' if m['name'].endswith(('.mp4','.webm','.mov')) else 'image'
            if m['kind']!=expected or not (STORAGE/m['name']).is_file():raise HTTPException(422,'Медиа не найдено')
        document,body,plain=render_content(payload.content,media)
        scheduled=payload.scheduled_at
        if scheduled:
            if not scheduled.tzinfo:raise HTTPException(422,'Для расписания требуется часовой пояс')
            scheduled=scheduled.astimezone(timezone.utc)
        async with database.sessions.begin() as s:
            p=await s.scalar(select(Publication).where(Publication.id==id).with_for_update()) if id else None
            if id and not p:raise HTTPException(404,'Публикация не найдена')
            if p and (payload.version is None or p.version!=payload.version):raise HTTPException(409,'Публикация изменена другим администратором. Сохраните текст локально и откройте её заново.')
            if p and p.status in ('deleted','archived'):raise HTTPException(409,'Сначала восстановите публикацию в черновики')
            if p and p.status=='scheduled' and scheduled and scheduled<=now():raise HTTPException(422,'Укажите будущую дату запланированной публикации')
            if p and p.status=='published' and (not payload.title.strip() or not (plain or media)):raise HTTPException(422,'Опубликованная статья не может быть пустой')
            n=await s.get(WebNews,id) if p else WebNews(title=payload.title,category=payload.section,published=False);s.add(n);await s.flush()
            if not p:p=Publication(id=n.id,status='draft',created_by=actor.id);s.add(p)
            if p.status=='scheduled' and not scheduled:p.status='draft'
            p.section=payload.section;p.content=json.dumps(document,ensure_ascii=False);p.body=body;p.plain=plain;p.media=json.dumps(media,ensure_ascii=False);p.cover=payload.cover;p.slideshow=payload.slideshow;p.pinned=payload.pinned;p.notify_clients=payload.notify_clients and payload.section not in ('faq','instructions');p.scheduled_at=scheduled;p.updated_at=now();p.updated_by=actor.id;p.version=(p.version or 0)+1
            n.title=payload.title
            await s.flush()
            from app.web.cabinet.help_content import remember_origin
            try:await remember_origin(s,p,payload.help_key)
            except ValueError as error:raise HTTPException(409,str(error)) from error
            await sync_visibility(s,p,n,settings)
            await SQLRepository(s).add(AdminAudit(actor_telegram_id=actor.telegram_id,action='publication_save',target=str(n.id),details='version='+str(p.version)))
            return await full(s,p,n)
    @router.post('/api/web/admin/publications/{id}/action')
    async def action(id:int,payload:ActionPayload,freeman_session:str|None=Cookie(default=None)):
        actor=await admin(freeman_session)
        async with database.sessions.begin() as s:
            p=await s.scalar(select(Publication).where(Publication.id==id).with_for_update());n=await s.get(WebNews,id)
            if not p:raise HTTPException(404,'Публикация не найдена')
            if p.version!=payload.version:raise HTTPException(409,'Публикация изменена. Обновите список.')
            op=payload.action
            if op=='duplicate':
                new=WebNews(title=('Копия — '+n.title)[:160],category=p.section,published=False);s.add(new);await s.flush()
                copy=Publication(id=new.id,section=p.section,status='draft',content=p.content,body=p.body,plain=p.plain,media=p.media,cover=p.cover,slideshow=p.slideshow,pinned=False,notify_clients=p.notify_clients,updated_at=now(),created_by=actor.id,updated_by=actor.id,version=1);s.add(copy);await s.flush();return await full(s,copy,new)
            if op=='publish':
                if p.status in ('archived','deleted'):raise HTTPException(409,'Сначала восстановите публикацию')
                if p.scheduled_at:
                    if p.scheduled_at.replace(tzinfo=p.scheduled_at.tzinfo or timezone.utc)<=now():raise HTTPException(422,'Дата публикации должна быть в будущем')
                    if not n.title.strip() or not (p.plain or json.loads(p.media)):raise HTTPException(422,'Заполните заголовок и содержание')
                    p.status='scheduled';await sync_visibility(s,p,n,settings)
                elif p.status!='published':await publish_record(s,p,n,settings)
            elif op=='archive':
                if p.status not in ('published','scheduled'):raise HTTPException(409,'Архивировать можно опубликованную или запланированную запись')
                p.status='archived';p.scheduled_at=None;await cancel_delivery(s,p)
            elif op=='delete':
                if p.status=='deleted':raise HTTPException(409,'Публикация уже удалена')
                p.status='deleted';p.deleted_at=now();p.scheduled_at=None;await cancel_delivery(s,p)
            elif op=='restore':
                if p.status not in ('deleted','archived'):raise HTTPException(409,'Нечего восстанавливать')
                p.status='draft';p.deleted_at=None;p.scheduled_at=None
            elif op=='purge':
                if p.status!='deleted':raise HTTPException(409,'Сначала переместите в удалённые')
                await cancel_delivery(s,p);await s.execute(delete(PublicationView).where(PublicationView.publication_id==id));await s.delete(p);await s.flush();await s.delete(n)
                await SQLRepository(s).add(AdminAudit(actor_telegram_id=actor.telegram_id,action='publication_purge',target=str(id),details='Media retained: may be referenced by other publications or deliveries'))
                return {'ok':True}
            elif op=='retry_notification':
                if p.status!='published' or not p.campaign_id:raise HTTPException(409,'Нет уведомления для повторной доставки')
                c=await s.get(Campaign,p.campaign_id);c.status='queued'
                await s.execute(update(Delivery).where(Delivery.campaign_id==p.campaign_id,Delivery.status=='failed').values(status='pending',attempts=0,available_at=now()))
            p.updated_at=now();p.updated_by=actor.id;p.version+=1;await sync_visibility(s,p,n,settings)
            await SQLRepository(s).add(AdminAudit(actor_telegram_id=actor.telegram_id,action='publication_'+op,target=str(id),details=p.status))
            return await full(s,p,n)
    @router.post('/api/web/admin/posts/media')
    async def upload(payload:MediaPayload,freeman_session:str|None=Cookie(default=None)):
        actor=await admin(freeman_session)
        types={'image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp','video/mp4':'.mp4','video/webm':'.webm','video/quicktime':'.mov'}
        if payload.mime not in types:raise HTTPException(422,'Поддерживаются JPG, PNG, WebP, MP4, WebM и MOV')
        try:raw=base64.b64decode(payload.data,validate=True)
        except (ValueError,binascii.Error):raise HTTPException(422,'Повреждённый файл')
        if not raw or len(raw)>20*1024*1024:raise HTTPException(422,'Каждый файл — до 20 МБ')
        valid=(payload.mime=='image/jpeg' and raw.startswith(b'\xff\xd8\xff') or payload.mime=='image/png' and raw.startswith(b'\x89PNG\r\n\x1a\n') or payload.mime=='image/webp' and raw[:4]==b'RIFF' and raw[8:12]==b'WEBP' or payload.mime in ('video/mp4','video/quicktime') and raw[4:8]==b'ftyp' or payload.mime=='video/webm' and raw.startswith(b'\x1aE\xdf\xa3'))
        if not valid:raise HTTPException(422,'Формат файла не соответствует содержимому')
        width=height=None;thumb=None
        if payload.mime.startswith('image/'):
            from PIL import Image,ImageOps
            from io import BytesIO
            try:
                with Image.open(BytesIO(raw)) as image:
                    image.verify()
                with Image.open(BytesIO(raw)) as image:
                    width,height=image.size
                    if width*height>40000000:raise ValueError('Image too large')
                    thumb=ImageOps.exif_transpose(image).convert('RGB');thumb.thumbnail((160,160))
            except Exception:raise HTTPException(422,'Изображение повреждено или слишком большое')
        STORAGE.mkdir(parents=True,exist_ok=True);name=secrets.token_hex(24)+types[payload.mime];(STORAGE/name).write_bytes(raw)
        if thumb:thumb.save(STORAGE/(name+'.thumb.jpg'),quality=80)
        async with database.sessions.begin() as s:
            s.add(PublicationMedia(name=name,kind='video' if payload.mime.startswith('video') else 'image',mime=payload.mime,size=len(raw),width=width,height=height,created_by=actor.id))
        return dict(name=name,kind='video' if payload.mime.startswith('video') else 'image',url='/api/web/posts/media/'+name,width=width,height=height,size=len(raw))
    @router.get('/api/web/posts/media/{name}')
    async def media_file(name:str,thumb:bool=False,freeman_session:str|None=Cookie(default=None)):
        actor=await current_user(freeman_session)
        if not re.fullmatch(MEDIA_RE,name) or not (STORAGE/name).is_file():raise HTTPException(404)
        async with database.sessions() as s:
            if not settings.is_owner(actor.telegram_id):
                visible=await s.scalar(select(Publication.id).where(Publication.status=='published',Publication.media.contains('"name": "'+name+'"')).limit(1))
                if not visible:raise HTTPException(404)
        path=STORAGE/name
        if thumb and name.endswith(('.jpg','.png','.webp')):
            path=STORAGE/(name+'.thumb.jpg')
            if not path.exists():
                from PIL import Image,ImageOps
                try:
                    with Image.open(STORAGE/name) as image:
                        image=ImageOps.exif_transpose(image).convert('RGB');image.thumbnail((160,160));image.save(path,quality=80)
                except Exception:raise HTTPException(404)
        return FileResponse(path,headers={'Cache-Control':'private, max-age=3600','X-Content-Type-Options':'nosniff'})
    return router
