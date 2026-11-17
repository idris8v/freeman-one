"""Bounded Telegram delivery for v2.1 callbacks; no business-operation retries."""
from pathlib import Path
import asyncio
import json
import re
import aiohttp
from sqlalchemy import select
from app.database.models import AIConversation,AIMessage,ChannelIdentity,AIEvent
from app.database.repositories import SQLRepository
from app.services.freeman_ai.security import decrypt,fingerprint
from .persistence import DurableRepository,PanelAdapter
from .presentation import fallback_keyboard,html_reply,verified_qr

class RateLimited(ValueError):
    def __init__(self, delay):
        super().__init__('telegram_rate_limited')
        self.delay=delay

class DeliveryRejected(ValueError):
    def __init__(self,status,*,rich_format=False):
        super().__init__('telegram_rejected')
        self.status=status
        self.rich_format=rich_format

async def request(client,token,method,data,files=()):
    url='https://api.telegram.org/bot'+token+'/'+method
    if files:
        form=aiohttp.FormData()
        for key,value in data.items():form.add_field(key,json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list,bool)) else str(value))
        for name,raw,extension,mime in files:
            form.add_field(name,raw,filename=name+'.'+extension,content_type=mime)
        outgoing=client.post(url,data=form,allow_redirects=False)
    else:outgoing=client.post(url,json=data,allow_redirects=False)
    async with outgoing as response:
        if response.status>=500:raise TimeoutError('telegram_delivery_outcome_unknown')
        try:
            result=await response.json()
        except (ValueError,aiohttp.ClientError):
            raise TimeoutError('telegram_delivery_outcome_unknown') from None
        if not isinstance(result,dict):
            raise TimeoutError('telegram_delivery_outcome_unknown')
        if response.status==429 and result.get('ok') is False and result.get('error_code')==429:
            delay=result.get('parameters',{}).get('retry_after')
            raise RateLimited(delay if type(delay) is int and 0<=delay<=86400 else 60)
        if result.get('ok') is False and response.status in (400,401,403,404) and result.get('error_code')==response.status:
            description=str(result.get('description','')).casefold()
            rich_format=method=='sendRichMessage' and response.status==400 and (
                "can't parse rich" in description or "failed to parse rich" in description
                or 'rich messages are not supported' in description
            )
            raise DeliveryRejected(response.status,rich_format=rich_format)
        if response.status!=200 or result.get('ok') is not True or 'result' not in result:
            raise TimeoutError('telegram_delivery_outcome_unknown')
        return result['result']

async def region_video(sessions):
    from fastapi import HTTPException
    from app.web.support_materials import region_video_path

    try:
        async with sessions() as session:
            path=await region_video_path(session)
        if path.stat().st_size>20*1024*1024:
            return None
        return await asyncio.to_thread(path.read_bytes)
    except (HTTPException,OSError):
        return None

async def send_reply(sessions,settings,token,chat_id,text,actions,*,connection=None):
    from anyio import to_thread
    rich=dict(html=html_reply(text,actions),skip_entity_detection=True);files=[];media=[]
    if any(a.get('type')=='guide_video' and a.get('material')=='appstore-region' for a in actions):
        raw=await region_video(sessions)
        if raw:
            name='region_guide'
            files.append((name,raw,'mp4','video/mp4'))
            media.append(dict(id=name,media=dict(type='video',media='attach://'+name,supports_streaming=True)))
            rich['html']='<video src="tg://video?id=region_guide"></video>'+rich['html']
    from .support_content import setup_media
    for action in actions:
        if action.get('type')!='setup_image':
            continue
        filename=setup_media(action.get('material'))
        if filename is None:
            continue
        file=Path(__file__).resolve().parents[3]/'web'/'static'/filename
        try:
            raw=await asyncio.to_thread(file.read_bytes)
        except FileNotFoundError:
            break
        files.append(('setup_guide',raw,'png','image/png'))
        media.append(dict(id='setup_guide',media=dict(type='photo',media='attach://setup_guide')))
        rich['html']='<img src="tg://photo?id=setup_guide"/>'+rich['html']
        break
    for action in actions:
        if action.get('type')!='agent_image':continue
        row=await DurableRepository(sessions,settings).get(action['token'])
        if not row or row['kind']!='qr':raise ValueError('qr_action_unavailable')
        async with PanelAdapter(sessions,settings,int(row['subject'])).context(row['key'],without_panel=True) as (s,b,c,api):
            from .interactions import verify_read_capability
            verify_read_capability(settings,row,c.subscription_url)
            raw=await to_thread.run_sync(verified_qr,c.subscription_url)
        name='qr'+str(len(files));files.append((name,raw,'png','image/png'))
        media.append(dict(id=name,media=dict(type='photo',media='attach://'+name)))
        rich['html']='<img src="tg://photo?id='+name+'"/>'+rich['html']
    if media:rich['media']=media
    payload=dict(chat_id=chat_id,rich_message=rich)
    if connection:payload['business_connection_id']=connection
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25),trust_env=False) as client:
        async def deliver():
            try:
                return await request(client,token,'sendRichMessage',payload,files)
            except DeliveryRejected as error:
                if not error.rich_format:
                    raise
                plain=text.replace('**','').replace('`','')
                native={'chat_id':chat_id}
                if connection:
                    native['business_connection_id']=connection
                keyboard=fallback_keyboard(actions)
                if keyboard['inline_keyboard']:
                    native['reply_markup']=keyboard
                if len(files)==1 and files[0][2]=='mp4' and len(plain)<=1024:
                    native.update(video='attach://'+files[0][0],caption=plain,supports_streaming=True)
                    return await request(client,token,'sendVideo',native,files)
                if len(files)==1 and files[0][2]=='png' and len(plain)<=1024:
                    native.update(photo='attach://'+files[0][0],caption=plain)
                    return await request(client,token,'sendPhoto',native,files)
                native['text']=plain
                return await request(client,token,'sendMessage',native)
        try:
            result=await deliver()
        except RateLimited as error:
            if error.delay>5:
                raise
            await asyncio.sleep(error.delay)
            result=await deliver()
        if not isinstance(result,dict) or type(result.get('message_id')) is not int:
            raise TimeoutError('telegram_delivery_outcome_unknown')
        return result

async def callback(sessions,settings,token,query,*,business_scope=None):
    match=re.fullmatch(r'f21:([A-Za-z0-9_-]{32}):([oyn])',str(query.get('data','')))
    if not match:return False
    action_token,choice=match.groups();decision={'o':'open','y':'confirm','n':'cancel'}[choice]
    from .integration import active
    row=await DurableRepository(sessions,settings).get(action_token)
    if not row:return False
    cid=int(row['subject']);chat=query.get('message',{}).get('chat',{});caller=query.get('from',{}).get('id')
    if chat.get('type')!='private' or type(caller) is not int or caller!=chat.get('id'):return False
    async with sessions() as s:
        if not await active(s):return False
        conv=await s.get(AIConversation,cid)
        if not conv or conv.mode!='ai':return False
        if business_scope:
            route=await SQLRepository(s).setting('support_agent_route:'+str(cid))
            saved=json.loads(decrypt(settings,route)) if route else None
            if saved!=business_scope or saved.get('chat_id')!=caller:return False
            if conv.channel!='support_agent_telegram':return False
            from app.services.freeman_ai.security import aware
            from app.database.models import now
            latest=await s.scalar(select(AIMessage.created_at).where(
                AIMessage.conversation_id==cid,AIMessage.role=='client',
            ).order_by(AIMessage.id.desc()).limit(1))
            if not latest or (now()-aware(latest)).total_seconds()>=23*3600:return False
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8),trust_env=False) as client:
                connection=await request(client,token,'getBusinessConnection',dict(
                    business_connection_id=saved['connection'],
                ))
            if (
                not connection.get('is_enabled')
                or connection.get('user',{}).get('id')!=saved['owner_id']
                or not connection.get('rights',{}).get('can_reply',connection.get('can_reply',False))
            ):return False
        else:
            from app.services.freeman_ai.service import channel_enabled
            if conv.channel!='telegram' or not await channel_enabled(s,settings,'telegram'):return False
            identity=await s.get(ChannelIdentity,conv.identity_id) if conv.identity_id else None
            if not identity or identity.channel!='telegram' or decrypt(settings,identity.encrypted_subject)!=str(caller):return False
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5),trust_env=False) as client:
        try:
            await request(client,token,'answerCallbackQuery',dict(callback_query_id=query['id'],text='Проверяем…'))
        except (DeliveryRejected,RateLimited,TimeoutError,aiohttp.ClientError):
            # An expired UI acknowledgement must not stop the update consumer.
            pass
    from .interactions import handle
    try:result=await handle(sessions,settings,action_token,cid,decision)
    except Exception:
        result=dict(text='⏳ Не удалось подтвердить результат. Не будем повторять изменение вслепую — проверим состояние.',actions=[])
    delivery_ref=action_token+':'+decision+':'+result.get('status','done')
    marker='agent_v21_delivery:'+fingerprint(settings,delivery_ref)
    async with sessions.begin() as s:
        from app.services.customer_identity import lock_nonce
        await lock_nonce(s,marker)
        repo=SQLRepository(s)
        if await repo.setting(marker):return True
        conv=await s.get(AIConversation,cid,populate_existing=True)
        if conv.mode!='ai':return True
        actions=[dict(type='agent_version',version='2.1')]+result['actions']
        digest=fingerprint(settings,'v21-callback:'+delivery_ref)
        previous=await s.scalar(select(AIMessage.id).where(AIMessage.source_hash==digest))
        if not previous:s.add(AIMessage(conversation_id=cid,source_hash=digest,generation=conv.generation,role='assistant',text=result['text'],actions=json.dumps(actions,ensure_ascii=False)))
        await repo.set_setting(marker,'sending')
    try:
        sent=await send_reply(sessions,settings,token,caller,result['text'],result['actions'],connection=business_scope['connection'] if business_scope else None)
        status='sent:'+str(sent['message_id'])
    except Exception:status='uncertain'
    async with sessions.begin() as s:
        await SQLRepository(s).set_setting(marker,status)
        s.add(AIEvent(conversation_id=cid,kind='agent.action_delivery',name=decision,status=status.split(':')[0]))
    return True

async def business_callback(agent,credential,scope,query):
    return await callback(agent.db.sessions,agent.settings,credential['token'],query,business_scope=scope)

async def bot_callback(query,screens):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    sessions=async_sessionmaker(screens.repo.session.bind,expire_on_commit=False)
    return await callback(sessions,screens.settings,screens.settings.bot_token,query.model_dump(by_alias=True,exclude_none=True))
