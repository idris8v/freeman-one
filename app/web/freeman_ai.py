"""Website/account/operator boundaries. No unauthenticated customer lookups."""
import json,secrets,re
from pathlib import Path
from typing import Literal
from fastapi import APIRouter,Request,HTTPException,Depends
from fastapi.responses import JSONResponse,FileResponse,PlainTextResponse,RedirectResponse,Response
from pydantic import BaseModel,Field
from sqlalchemy import select,func,or_,cast,String
from app.database.models import AIConversation,AIMessage,AIJob,AIKnowledge,AIEvent,User,AppSetting,now
from app.database.repositories import SQLRepository
from app.services.freeman_ai import service,identity
from app.services.freeman_ai.security import fingerprint,redact,aware
from app.services.customer_identity import authenticated
from app.web.assistant_reports import present as present_reports

class AttachmentInput(BaseModel):
    name:str=Field(min_length=1,max_length=120)
    data:str=Field(min_length=1,max_length=5600000)
class RenameInput(BaseModel):
    title:str=Field(min_length=1,max_length=80)
class MessageInput(BaseModel):
    text:str=Field(min_length=1,max_length=3500)
    files:list[AttachmentInput]=Field(default_factory=list,max_length=3)
    images:list[str]=Field(default_factory=list,max_length=4)
    request_id:str=Field(min_length=16,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
from app.web.cabinet.schemas import SupportMessagePayload
class CustomerReplyInput(SupportMessagePayload):
    request_id:str=Field(min_length=16,max_length=64,pattern=r'^[a-zA-Z0-9-]+$')
class ModeInput(BaseModel):mode:str
class LinkInput(BaseModel):token:str=Field(min_length=40,max_length=100)
class KnowledgeInput(BaseModel):
    title:str=Field(min_length=1,max_length=160)
    text:str=Field(min_length=1,max_length=6000)
    enabled:bool=False
class ProposalInput(BaseModel):
    message_id:int
    index:int=Field(ge=0,le=20)
class ScheduleInput(ProposalInput):
    due_at:str=Field(max_length=50)
class ContextInput(BaseModel):
    mode:Literal['service','open']='service'
class TaskCreateInput(BaseModel):
    conversation_id:int|None=Field(default=None,ge=1,strict=True)
    prompt:str=Field(min_length=1,max_length=2000)
    due_at:str=Field(max_length=50)
    kind:Literal['report','reminder']='report'
class ConfigInput(BaseModel):enabled:bool

def ai_router(database,settings,current_user):
    async def private_ai(request:Request):
        if request.url.path.startswith('/api/ai/') and request.url.path!='/api/ai/status':raise HTTPException(403,'AI-ассистент доступен только администратору. Обратитесь в поддержку в личном кабинете.')
    router=APIRouter(dependencies=[Depends(private_ai)]);static=Path(__file__).parent/'static'
    from app.web.assistant_voice import Dictation
    dictation=Dictation()
    def origin(request):
        if request.headers.get('sec-fetch-site')=='cross-site' or request.headers.get('origin') not in (None,settings.web_base_url.rstrip('/')):raise HTTPException(403,'Запрос должен быть отправлен с сайта Freeman ONE.')
    async def actor(request,owner=False):
        user=await current_user(request.cookies.get('freeman_session'))
        if not (settings.is_owner(user.telegram_id) if owner else settings.can_admin_action(user.telegram_id,'admin:t:home')):raise HTTPException(403,'Нет доступа.')
        return user
    async def web_conversation(s,request,response):
        user=await authenticated(s,request,settings)
        browser=request.cookies.get('freeman_ai_browser','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',browser):
            browser=secrets.token_urlsafe(32);response.set_cookie('freeman_ai_browser',browser,max_age=180*86400,secure=True,httponly=True,samesite='lax',path='/')
        return await service.conversation(s,settings,user_id=user.id if user else None,browser=browser)
    async def owned(s,request,cid):
        conv=await s.get(AIConversation,cid)
        user=await authenticated(s,request,settings)
        browser=request.cookies.get('freeman_ai_browser','')
        valid=conv and conv.channel=='website' and (user and conv.user_id==user.id or not conv.user_id and browser and conv.browser_hash==fingerprint(settings,'browser:'+browser))
        if not valid:raise HTTPException(404,'Переписка не найдена.')
        return conv
    @router.get('/ai')
    async def page():return RedirectResponse('/',status_code=303,headers={'Cache-Control':'no-store'})
    @router.get('/admin/assistant')
    async def personal_page(request:Request):
        await actor(request,owner=True);return FileResponse(static/'freeman-assistant.html',headers={'Cache-Control':'no-store'})
    @router.get('/admin/ai')
    async def admin_page(request:Request):
        await actor(request);return FileResponse(static/'freeman-ai.html',headers={'Cache-Control':'no-store'})
    @router.get('/ai/checkout/{token}')
    async def checkout_link(token:str,request:Request):
        raise HTTPException(410,'Откройте покупку подписки в личном кабинете.')
    @router.get('/api/ai/status')
    async def status():
        async with database.sessions() as s:return JSONResponse({'enabled':False,'name':'Freeman AI'},headers={'Cache-Control':'no-store'})
    @router.post('/api/ai/conversations')
    async def start(request:Request):
        origin(request);response=JSONResponse({})
        async with database.sessions.begin() as s:
            if not await service.channel_enabled(s,settings,'website'):raise HTTPException(503,'Freeman AI пока не включён. Воспользуйтесь обычной поддержкой.')
            from app.services.freeman_ai.limits import throttle
            try:await throttle(s,settings,'website:'+(request.client.host if request.client else 'unknown'))
            except ValueError as e:raise HTTPException(429,str(e))
            conv=await web_conversation(s,request,response)
            data={'id':conv.id,'mode':conv.mode,'messages':present_reports(await service.messages(s,conv)),'verified':bool(conv.user_id)}
        response.body=json.dumps(data,ensure_ascii=False).encode();response.headers['content-length']=str(len(response.body));response.headers['Cache-Control']='no-store';return response
    @router.get('/api/ai/conversations/{cid}')
    async def detail(cid:int,request:Request):
        async with database.sessions() as s:
            c=await owned(s,request,cid);return JSONResponse({'id':c.id,'mode':c.mode,'messages':present_reports(await service.messages(s,c))},headers={'Cache-Control':'no-store'})
    @router.post('/api/ai/conversations/{cid}/messages')
    async def send(cid:int,payload:MessageInput,request:Request):
        origin(request)
        async with database.sessions.begin() as s:
            if not await service.channel_enabled(s,settings,'website'):raise HTTPException(503,'AI временно отключён.')
            c=await owned(s,request,cid)
            try:m=await service.append(s,settings,c,payload.text,'web:'+str(cid)+':'+payload.request_id)
            except ValueError as e:raise HTTPException(429,str(e))
            return {'id':m.id,'mode':c.mode}
    @router.post('/api/ai/identities/link')
    async def link(payload:LinkInput,request:Request):
        origin(request);user=await current_user(request.cookies.get('freeman_session'))
        async with database.sessions.begin() as s:
            try:await identity.consume_challenge(s,settings,user,payload.token)
            except (ValueError,PermissionError):raise HTTPException(409,'Не удалось подтвердить привязку. Запросите новое подтверждение в канале.')
        return {'ok':True}
    @router.post('/api/web/admin/assistant')
    async def owner_start(request:Request,cid:int=0,new:bool=False):
        origin(request);u=await actor(request,owner=True)
        from app.services.freeman_ai import owner
        async with database.sessions.begin() as s:
            c=await s.get(AIConversation,cid) if cid else await owner.conversation(s,settings,u,force_new=new)
            if not c or c.user_id!=u.id or c.channel!='owner' or c.mode=='deleted':raise HTTPException(404,'Чат недоступен.')
            return {'id':c.id,'mode':c.mode,'channel':'owner','context_mode':await owner.context_mode(s,c),'messages':present_reports(await service.messages(s,c))}
    @router.get('/api/web/admin/assistant/images/{iid}')
    async def owner_image(iid:int,request:Request):
        u=await actor(request,owner=True)
        from app.database.models import AIOwnerImage
        from app.services.freeman_ai.security import decrypt
        import base64
        async with database.sessions() as s:
            image=await s.get(AIOwnerImage,iid)
            m=await s.get(AIMessage,image.message_id) if image else None
            c=await s.get(AIConversation,m.conversation_id) if m else None
            if not c or c.channel!='owner' or c.user_id!=u.id or c.mode=='deleted':raise HTTPException(404,'Изображение недоступно.')
            data=decrypt(settings,image.encrypted_data);header,encoded=data.split(',',1)
            return Response(base64.b64decode(encoded),media_type=header[5:].split(';')[0],headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    @router.get('/api/web/admin/assistant/chats')
    async def owner_chats(request:Request,q:str=''):
        u=await actor(request,owner=True)
        async with database.sessions() as s:
            query=select(AIConversation).where(AIConversation.channel=='owner',AIConversation.user_id==u.id,AIConversation.mode!='deleted')
            if q:
                pattern='%'+q[:100].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
                matched=select(AIMessage.conversation_id).where(AIMessage.text.ilike(pattern,escape='\\'))
                titles=select(AppSetting.key).where(AppSetting.value.ilike(pattern,escape='\\'))
                from sqlalchemy import cast,String,literal
                query=query.where(or_(AIConversation.id.in_(matched),(literal('assistant_title:')+cast(AIConversation.id,String)).in_(titles)))
            first_text=select(AIMessage.text).where(AIMessage.conversation_id==AIConversation.id,AIMessage.role=='client').order_by(AIMessage.id).limit(1).scalar_subquery()
            rows=list((await s.execute(query.add_columns(first_text).order_by(AIConversation.updated_at.desc()).limit(100))).all())
            title_keys=['assistant_title:'+str(c.id) for c,_ in rows]
            titles=dict((await s.execute(select(AppSetting.key,AppSetting.value).where(AppSetting.key.in_(title_keys)))).all()) if title_keys else {}
            items=[dict(id=c.id,title=titles.get('assistant_title:'+str(c.id)) or (first or 'Новый чат')[:60],updated_at=aware(c.updated_at).isoformat()) for c,first in rows]
            return JSONResponse({'items':items},headers={'Cache-Control':'no-store'})
    @router.patch('/api/web/admin/assistant/chats/{cid}/context')
    async def context_chat(cid:int,payload:ContextInput,request:Request):
        origin(request);u=await actor(request,owner=True)
        async with database.sessions.begin() as s:
            c=await s.get(AIConversation,cid)
            if not c or c.channel!='owner' or c.user_id!=u.id or c.mode=='deleted':raise HTTPException(404)
            await SQLRepository(s).set_setting('assistant_context:'+str(cid),payload.mode)
            s.add(AIEvent(conversation_id=c.id,kind='owner.context',name=payload.mode))
        return {'context_mode':payload.mode}
    @router.patch('/api/web/admin/assistant/chats/{cid}')
    async def rename_chat(cid:int,payload:RenameInput,request:Request):
        origin(request);u=await actor(request,owner=True)
        async with database.sessions.begin() as s:
            c=await s.get(AIConversation,cid)
            if not c or c.channel!='owner' or c.user_id!=u.id or c.mode=='deleted':raise HTTPException(404)
            await SQLRepository(s).set_setting('assistant_title:'+str(cid),redact(payload.title).strip())
            s.add(AIEvent(kind='owner.chat.renamed',name=str(cid),details=json.dumps({'actor_id':u.id})))
        return {'ok':True}
    @router.get('/api/web/admin/assistant/files')
    async def owner_files(request:Request):
        u=await actor(request,owner=True)
        from app.database.models import AIOwnerFile
        async with database.sessions() as s:
            rows=await s.scalars(select(AIOwnerFile).join(AIMessage,AIOwnerFile.message_id==AIMessage.id).join(AIConversation,AIMessage.conversation_id==AIConversation.id).where(AIConversation.user_id==u.id,AIConversation.channel=='owner',AIConversation.mode!='deleted').order_by(AIOwnerFile.id.desc()).limit(100))
            return {'items':[{'type':'file','name':r.name,'size':r.size,'mime':r.mime,'url':'/api/web/admin/assistant/files/'+str(r.id)} for r in rows if not r.mime.startswith('audio/')]}
    @router.get('/api/web/admin/assistant/files/{fid}')
    async def owner_file(fid:int,request:Request,download:bool=False):
        u=await actor(request,owner=True)
        from app.database.models import AIOwnerFile
        from app.services.freeman_ai.security import decrypt
        import base64
        from urllib.parse import quote
        async with database.sessions() as s:
            f=await s.get(AIOwnerFile,fid);m=await s.get(AIMessage,f.message_id) if f else None;c=await s.get(AIConversation,m.conversation_id) if m else None
            if not c or c.channel!='owner' or c.user_id!=u.id or c.mode=='deleted':raise HTTPException(404)
            if f.mime.startswith('audio/'):raise HTTPException(404)
            d=json.loads(decrypt(settings,f.encrypted_data))
            return Response(base64.b64decode(d['data']),media_type=f.mime,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Disposition':"attachment"+"; filename*=UTF-8''"+quote(f.name)})
    @router.delete('/api/web/admin/assistant/chats/{cid}')
    async def delete_owner_chat(cid:int,request:Request):
        origin(request);u=await actor(request,owner=True)
        from sqlalchemy import delete,update
        from app.database.models import AIOwnerTask,AIOwnerImage,AIOwnerFile
        from app.services.customer_identity import lock_nonce
        async with database.sessions.begin() as s:
            await lock_nonce(s,'ai:scheduled-conversation:'+str(cid))
            c=await s.get(AIConversation,cid,with_for_update=True)
            if not c or c.channel!='owner' or c.user_id!=u.id:raise HTTPException(404,'Чат недоступен.')
            c.mode='deleted';c.generation+=1;c.summary=''
            mids=select(AIMessage.id).where(AIMessage.conversation_id==cid)
            await s.execute(update(AIJob).where(AIJob.message_id.in_(mids)).values(status='canceled'))
            await s.execute(delete(AIOwnerImage).where(AIOwnerImage.message_id.in_(mids)))
            await s.execute(delete(AIOwnerFile).where(AIOwnerFile.message_id.in_(mids)))
            await s.execute(update(AIMessage).where(AIMessage.conversation_id==cid).values(text='[Чат удалён]',actions='[]'))
            await s.execute(update(AIOwnerTask).where(AIOwnerTask.conversation_id==cid).values(status='canceled',prompt='[Чат удалён]',finished_at=now()))
            s.add(AIEvent(kind='owner.chat.deleted',name=str(cid),details=json.dumps({'actor_id':u.id})))
        return {'deleted':True}
    @router.get('/api/web/admin/assistant/tasks')
    async def owner_tasks(request:Request):
        u=await actor(request,owner=True)
        from app.database.models import AIOwnerTask
        from app.services.freeman_ai.tasks import present
        async with database.sessions() as s:
            rows=await s.scalars(select(AIOwnerTask).join(AIConversation,AIConversation.id==AIOwnerTask.conversation_id).where(AIOwnerTask.owner_id==u.id,AIConversation.user_id==u.id,AIConversation.channel=='owner',AIConversation.mode=='ai').order_by(AIOwnerTask.id.desc()).limit(50))
            return JSONResponse({'items':[present(t) for t in rows]},headers={'Cache-Control':'no-store'})
    @router.post('/api/web/admin/assistant/tasks/create')
    async def owner_create_task(payload:TaskCreateInput,request:Request):
        origin(request);u=await actor(request,owner=True)
        if not payload.prompt.strip():raise HTTPException(422,'Опишите задачу.')
        from app.services.freeman_ai import owner,tasks
        async with database.sessions.begin() as s:
            c=await s.get(AIConversation,payload.conversation_id) if payload.conversation_id else await owner.conversation(s,settings,u)
            if not c or c.user_id!=u.id or c.channel!='owner' or c.mode!='ai':raise HTTPException(404,'Чат недоступен.')
            try:return await tasks.execute(s,settings,c,'schedule_reminder' if payload.kind=='reminder' else 'schedule_report',{'prompt':payload.prompt,'due_at':payload.due_at})
            except (ValueError,PermissionError) as e:raise HTTPException(422,str(e))
    @router.post('/api/web/admin/assistant/dictation')
    async def owner_dictation(request:Request):
        origin(request);u=await actor(request,owner=True)
        return await dictation.response(request,settings,u.id)
    @router.post('/api/web/admin/assistant/tasks/{tid}/cancel')
    async def owner_cancel_task(tid:int,request:Request):
        origin(request);u=await actor(request,owner=True)
        from app.services.freeman_ai import owner,tasks
        async with database.sessions.begin() as s:
            c=await owner.conversation(s,settings,u)
            try:return await tasks.execute(s,settings,c,'cancel_task',{'task_id':str(tid)})
            except (PermissionError,ValueError):raise HTTPException(404,'Задача недоступна.')
    @router.post('/api/web/admin/assistant/tasks/schedule')
    async def owner_schedule(payload:ScheduleInput,request:Request):
        origin(request);u=await actor(request,owner=True)
        from app.services.freeman_ai.tasks import schedule_operation
        async with database.sessions.begin() as s:
            try:return await schedule_operation(s,settings,u,payload.message_id,payload.index,payload.due_at)
            except (PermissionError,ValueError) as e:raise HTTPException(422,str(e))
    @router.post('/api/web/admin/assistant/confirm')
    async def owner_confirm(payload:ProposalInput,request:Request):
        origin(request);u=await actor(request,owner=True)
        from app.services.freeman_ai import owner
        async with database.sessions.begin() as s:
            try:kid=await owner.confirm(s,settings,u,payload.message_id,payload.index)
            except PermissionError:raise HTTPException(404,'Предложение недоступно.')
            except ValueError as e:raise HTTPException(422,str(e)[:1500])
            return {'result':kid,'completed':True} if isinstance(kid,dict) else {'id':kid,'saved':True}
    @router.get('/api/web/admin/ai')
    async def dashboard(request:Request,channel:str='',mode:str='',q:str='',page:int=1):
        u=await actor(request)
        async with database.sessions() as s:
            from app.web.ai_inbox import inbox
            data=await inbox(s,settings,page,channel,mode,q)
            data['owner_access']=settings.is_owner(u.telegram_id)
            data['viewer_id']=u.id
            return JSONResponse(data,headers={'Cache-Control':'no-store'})
    @router.get('/api/web/admin/assistant/chats/{cid}')
    @router.get('/api/web/admin/ai/conversations/{cid}')
    async def admin_detail(cid:int,request:Request):
        await actor(request)
        async with database.sessions() as s:
            c=await s.get(AIConversation,cid)
            if not c or c.mode=='deleted':raise HTTPException(404,'Не найдено.')
            if ('/assistant/' in request.url.path)!=(c.channel=='owner'):raise HTTPException(404,'Не найдено.')
            if c.channel=='owner':
                u=await actor(request,owner=True)
                if c.user_id!=u.id:raise HTTPException(404,'Не найдено.')
            data={'id':c.id,'mode':c.mode,'channel':c.channel,'context_mode':await SQLRepository(s).setting('assistant_context:'+str(c.id),'service'),'ticket_id':c.ticket_id,'messages':present_reports(await service.messages(s,c))}
            if c.channel!='owner':
                from app.web.ai_customer_chat import workspace
                data.update(await workspace(s,settings,c,await actor(request),data['messages']))
            return JSONResponse(data,headers={'Cache-Control':'no-store'})
    from app.web.ai_customer_chat import reply_handler
    send_customer_reply=reply_handler(database,settings,current_user)
    @router.post('/api/web/admin/ai/conversations/{cid}/reply')
    async def customer_reply(cid:int,payload:CustomerReplyInput,request:Request):
        origin(request);operator=await actor(request)
        return await send_customer_reply(cid,payload,operator,request.cookies.get('freeman_session'))
    @router.post('/api/web/admin/ai/conversations/{cid}/mode')
    async def mode(cid:int,payload:ModeInput,request:Request):
        origin(request);operator=await actor(request)
        async with database.sessions.begin() as s:
            c=await s.get(AIConversation,cid)
            if not c or c.mode=='deleted':raise HTTPException(404,'Не найдено.')
            if ('/assistant/' in request.url.path)!=(c.channel=='owner'):raise HTTPException(404,'Не найдено.')
            if c.channel=='owner':
                u=await actor(request,owner=True)
                if c.user_id!=u.id:raise HTTPException(404,'Не найдено.')
            if c.channel=='owner':raise HTTPException(409,'В личном чате нет переключения оператора.')
            from app.services.freeman_ai import customer_config
            if payload.mode=='ai':
                if c.channel=='support_agent_telegram':
                    from app.services.freeman_ai.v21.integration import active as v21_active
                    configured=customer_config.configured(settings) and await v21_active(s) and await SQLRepository(s).setting('support_agent_telegram_enabled','0')=='1'
                else:
                    configured=await customer_config.enabled(s,settings)
                if not configured:raise HTTPException(409,'Клиентский агент пока не настроен или выключен.')
            try:
                if payload.mode=='closed' and c.ticket_id:
                    from app.services.tickets import TicketService
                    await TicketService(SQLRepository(s),settings).set_status(c.ticket_id,operator,'closed',True)
                elif c.channel=='support_agent_telegram' and payload.mode=='ai':
                    from app.web.support_agent import resume_business_conversation
                    await resume_business_conversation(s,settings,c)
                else:await service.switch_mode(s,settings,c,payload.mode)
            except ValueError:raise HTTPException(422,'Некорректный статус.')
        return {'ok':True}
    @router.get('/api/web/admin/ai/tickets/{tid}')
    async def legacy_ticket(tid:int,request:Request):
        viewer=await actor(request)
        from app.database.models import Ticket
        from app.web.ai_inbox import ticket_workspace
        async with database.sessions() as session:
            ticket=await session.get(Ticket,tid)
            if not ticket:raise HTTPException(404,'Обращение недоступно.')
            linked=await session.scalar(select(AIConversation.id).where(AIConversation.ticket_id==tid,AIConversation.channel!='owner',AIConversation.mode!='deleted').order_by(AIConversation.id.desc()).limit(1))
            if linked:return JSONResponse({'redirect_conversation_id':linked},headers={'Cache-Control':'no-store'})
            return JSONResponse(await ticket_workspace(session,settings,ticket,viewer),headers={'Cache-Control':'no-store'})
    @router.post('/api/web/admin/ai/tickets/{tid}/reply')
    async def legacy_reply(tid:int,payload:CustomerReplyInput,request:Request):
        origin(request);viewer=await actor(request)
        if payload.client_mode or payload.ticket_id is not None:raise HTTPException(422,'Обращение определяется по адресу запроса.')
        from app.web.cabinet.support import support_router
        from types import SimpleNamespace
        router_support=support_router(SimpleNamespace(database=database,settings=settings,current_user=current_user))
        handler=next(r.endpoint for r in router_support.routes if r.path=='/api/web/support/messages')
        return await handler(payload.model_copy(update={'ticket_id':tid}),freeman_session=request.cookies.get('freeman_session'))
    @router.post('/api/web/admin/ai/tickets/{tid}/mode')
    async def legacy_mode(tid:int,payload:ModeInput,request:Request):
        origin(request);viewer=await actor(request)
        from app.database.models import Ticket,TicketMessage
        from app.services.tickets import TicketService
        from app.services.freeman_ai import customer_config
        async with database.sessions.begin() as session:
            await session.execute(__import__('sqlalchemy').update(Ticket).where(Ticket.id==tid).values(updated_at=Ticket.updated_at))
            ticket=await session.get(Ticket,tid)
            if not ticket:raise HTTPException(404,'Обращение недоступно.')
            if payload.mode=='ai':
                if not await customer_config.enabled(session,settings):raise HTTPException(409,'Сначала настройте и включите отдельное подключение клиентского агента.')
                if ticket.status=='closed':raise HTTPException(409,'Сначала возобновите обращение.')
                last=await session.scalar(select(TicketMessage).where(TicketMessage.ticket_id==tid,TicketMessage.role=='client').order_by(TicketMessage.id.desc()).limit(1))
                conv=await service.attach_ticket(session,settings,ticket,last.source if last else '',force=True)
                if not conv:raise HTTPException(409,'Канал пока не готов для клиентского агента.')
                if conv.mode!='ai':
                    await service.switch_mode(session,settings,conv,'ai')
                return {'ok':True,'conversation_id':conv.id}
            await TicketService(SQLRepository(session),settings).set_status(tid,viewer,'closed'if payload.mode=='closed'else'in_progress',True)
        return {'ok':True}
    @router.post('/api/web/admin/ai/config')
    async def customer_configure(payload:ConfigInput,request:Request):
        origin(request);await actor(request,owner=True)
        from app.services.freeman_ai import customer_config
        if payload.enabled and not (getattr(settings,'freeman_customer_ai_enabled',False) and customer_config.configured(settings)):raise HTTPException(409,'Отдельное API-подключение клиентов ещё не настроено на сервере.')
        async with database.sessions.begin() as session:
            await SQLRepository(session).set_setting('freeman_customer_ai_enabled','1'if payload.enabled else'0')
            session.add(AIEvent(kind='customer.config',name='enabled'if payload.enabled else'disabled'))
        return {'ok':True}
    @router.post('/api/web/admin/assistant/chats/{cid}/messages')
    @router.post('/api/web/admin/ai/conversations/{cid}/messages')
    async def admin_send(cid:int,payload:MessageInput,request:Request):
        origin(request);operator=await actor(request)
        async with database.sessions.begin() as s:
            c=await s.get(AIConversation,cid)
            if not c or c.mode=='deleted':raise HTTPException(404,'Не найдено.')
            if ('/assistant/' in request.url.path)!=(c.channel=='owner'):raise HTTPException(404,'Не найдено.')
            if c.channel=='owner':
                u=await actor(request,owner=True)
                if c.user_id!=u.id:raise HTTPException(404,'Не найдено.')
            if c.channel=='owner':
                if not await service.enabled(s,settings):raise HTTPException(503,'AI временно отключён.')
                try:
                    from app.services.freeman_ai.images import validate,store
                    for image in payload.images:validate(image)
                    from app.services.freeman_ai import files as attachments
                    for file in payload.files:attachments.decode(file.model_dump())
                    raw=payload.text.casefold()
                    explicit=any(x in raw for x in ('добав','сохран','обнов','запиш','внес','измени','создай')) and any(x in raw for x in ('баз','инструкц'))
                    actions=[{'type':'knowledge_write_authorized'}] if explicit else []
                    m=await service.append(s,settings,c,payload.text,'owner:'+str(c.id)+':'+payload.request_id,actions=actions)
                    await store(s,settings,m.id,payload.images)
                    await attachments.store(s,settings,m.id,[f.model_dump() for f in payload.files])
                except ValueError as e:raise HTTPException(429,str(e))
            elif c.ticket_id:
                from app.services.tickets import TicketService
                try:await TicketService(SQLRepository(s),settings).input('admin:t:input:reply:'+str(c.ticket_id),operator,payload.text,'ai-operator:'+str(cid)+':'+payload.request_id)
                except ValueError as e:raise HTTPException(409,str(e))
            else:
                await service.switch_mode(s,settings,c,'human')
                await service.append(s,settings,c,payload.text,'operator:'+str(cid)+':'+payload.request_id,'operator')
        return {'ok':True}
    @router.get('/api/web/admin/ai/knowledge')
    async def knowledge(request:Request):
        await actor(request)
        async with database.sessions() as s:return {'items':[dict(id=k.id,title=k.title,text=k.text,enabled=k.enabled,version=k.version) for k in await s.scalars(select(AIKnowledge).order_by(AIKnowledge.id.desc()))]}
    @router.post('/api/web/admin/ai/knowledge')
    async def create_article(payload:KnowledgeInput,request:Request):
        origin(request);await actor(request,owner=True)
        async with database.sessions.begin() as s:
            k=AIKnowledge(title=redact(payload.title),text=redact(payload.text),enabled=payload.enabled);s.add(k);await s.flush();return {'id':k.id}
    @router.put('/api/web/admin/ai/knowledge/{kid}')
    async def edit_article(kid:int,payload:KnowledgeInput,request:Request):
        origin(request);await actor(request,owner=True)
        async with database.sessions.begin() as s:
            k=await s.get(AIKnowledge,kid)
            if not k:raise HTTPException(404,'Не найдено.')
            k.title=redact(payload.title);k.text=redact(payload.text);k.enabled=payload.enabled;k.version+=1;k.updated_at=now()
        return {'ok':True}
    @router.delete('/api/web/admin/ai/knowledge/{kid}',status_code=204)
    async def delete_article(kid:int,request:Request):
        origin(request);await actor(request,owner=True)
        async with database.sessions.begin() as s:
            k=await s.get(AIKnowledge,kid)
            if not k:raise HTTPException(404,'Запись не найдена.')
            await s.delete(k)
    @router.get('/api/web/admin/assistant/chats/{cid}/live')
    async def assistant_live(cid:int,request:Request):
        u=await actor(request,owner=True)
        from app.web.assistant_live import live_response
        return await live_response(database,request,cid,u.id,actor)
    @router.get('/api/web/admin/assistant/chats/{cid}/messages/{mid}/tables/{index}.xlsx')
    async def assistant_excel(cid:int,mid:int,index:int,request:Request):
        u=await actor(request,owner=True)
        from app.web.assistant_reports import tables,commentary,workbook
        from starlette.concurrency import run_in_threadpool
        async with database.sessions() as s:
            c=await s.get(AIConversation,cid);m=await s.get(AIMessage,mid)
            if not c or c.user_id!=u.id or c.channel!='owner' or c.mode=='deleted' or not m or m.conversation_id!=cid or m.role!='assistant':raise HTTPException(404,'Отчёт недоступен.')
            actions=json.loads(m.actions);items=tables(m.text,actions);stamp=aware(m.created_at)
        if not 0<=index<len(items):raise HTTPException(404,'Таблица не найдена.')
        try:data=await run_in_threadpool(workbook,items[index],commentary(actions),stamp)
        except ValueError as error:raise HTTPException(422,str(error))
        return Response(data,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':f'attachment; filename="freeman-report-{mid}-{index+1}.xlsx"','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    @router.get('/api/web/admin/assistant/status')
    async def assistant_status(request:Request):
        await actor(request,owner=True)
        from app.web.assistant_live import available
        async with database.sessions() as s:return {'enabled':await service.enabled(s,settings),'model':settings.freeman_ai_model,'streaming':available()}
    @router.get('/api/web/admin/assistant/chats/{cid}/usage')
    async def assistant_usage(cid:int,request:Request):
        u=await actor(request,owner=True)
        from app.web.assistant_usage import conversation_usage
        async with database.sessions() as s:
            c=await s.get(AIConversation,cid)
            if not c or c.user_id!=u.id or c.channel!='owner' or c.mode=='deleted':
                raise HTTPException(404,'Чат недоступен.')
            return JSONResponse(await conversation_usage(s,cid),headers={'Cache-Control':'no-store'})
    @router.post('/api/web/admin/assistant/config')
    async def config(payload:ConfigInput,request:Request):
        origin(request);await actor(request,owner=True)
        if payload.enabled and not (settings.freeman_ai_enabled and settings.freeman_ai_model and settings.freeman_ai_openai_key):raise HTTPException(409,'Сначала настройте модель и ключ на сервере. Секреты не вводятся в браузере.')
        async with database.sessions.begin() as s:
            await SQLRepository(s).set_setting('freeman_ai_enabled','1' if payload.enabled else '0');s.add(AIEvent(kind='config',name='enabled' if payload.enabled else 'disabled'))
        return {'ok':True}
    @router.get('/api/web/admin/ai/events')
    async def events(request:Request):
        u=await actor(request)
        async with database.sessions() as s:return {'items':[dict(id=e.id,kind=e.kind,name=e.name,status=e.status,details=json.loads(e.details),created_at=aware(e.created_at).isoformat()) for e in await s.scalars(select(AIEvent).where(((AIEvent.conversation_id.is_(None)) | AIEvent.conversation_id.in_(select(AIConversation.id).where(AIConversation.channel!='owner'))),~AIEvent.kind.like('owner.%')).order_by(AIEvent.id.desc()).limit(100))]}
    @router.get('/api/ai/whatsapp/webhook')
    async def verify(request:Request):
        import hmac
        expected=settings.freeman_ai_whatsapp_verify_token
        if not expected or request.query_params.get('hub.mode')!='subscribe' or not hmac.compare_digest(request.query_params.get('hub.verify_token',''),expected):raise HTTPException(403,'Invalid verification')
        challenge=request.query_params.get('hub.challenge','')
        if not challenge.isdigit() or len(challenge)>200:raise HTTPException(400,'Invalid challenge')
        return PlainTextResponse(challenge)
    @router.post('/api/ai/whatsapp/webhook')
    async def webhook(request:Request):
        from app.services.freeman_ai.channels import valid_signature,ingest_whatsapp
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>131072:raise HTTPException(413,'Webhook too large')
        if not valid_signature(settings,bytes(raw),request.headers.get('x-hub-signature-256')):raise HTTPException(403,'Invalid signature')
        try:payload=json.loads(raw)
        except ValueError:raise HTTPException(400,'Invalid JSON')
        async with database.sessions.begin() as s:
            if not await service.channel_enabled(s,settings,'whatsapp'):raise HTTPException(503,'Channel not enabled')
            try:count=await ingest_whatsapp(s,settings,payload)
            except (ValueError,TypeError,KeyError):raise HTTPException(400,'Invalid event')
        return {'accepted':count}
    return router
