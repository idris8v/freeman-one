"""Official channel boundaries; no personal-account scraping or arbitrary URLs."""
import json,hmac,hashlib,re
from html import escape
from datetime import datetime,timezone,timedelta
import aiohttp
from sqlalchemy import select
from app.database.models import ChannelIdentity,AIMessage,AIConversation,AIOutbox,AIEvent,now
from .security import decrypt,encrypt,aware,fingerprint
from .identity import channel_identity
from .service import conversation,append,enabled,handoff,channel_enabled

def telegram_link_button(action):
    from app.web.support_message_actions import safe_url
    url=safe_url(action.get('url'))
    label=action.get('label')
    if not url or not isinstance(label,str) or not label.strip():return None
    return '<tg-button-row><tg-button type="url" url="'+escape(url,quote=True)+'">'+escape(label[:120])+'</tg-button></tg-button-row>'

def valid_signature(settings,raw,signature):
    secret=settings.freeman_ai_meta_app_secret
    return bool(secret and hmac.compare_digest('sha256='+hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest(),signature or ''))

async def ingest_whatsapp(session,settings,payload):
    if payload.get('object')!='whatsapp_business_account':raise ValueError('Invalid webhook object')
    accepted=0
    for entry in payload.get('entry',[])[:20]:
        for change in entry.get('changes',[])[:20]:
            value=change.get('value',{})
            if value.get('metadata',{}).get('phone_number_id')!=settings.freeman_ai_whatsapp_phone_id:continue
            for event in value.get('messages',[])[:20]:
                event_id=event.get('id','');subject=event.get('from','')
                if not event_id or len(event_id)>200 or not re.fullmatch(r'\d{1,20}',subject):continue
                digest=fingerprint(settings,'message:whatsapp:'+event_id)
                if await session.scalar(select(AIMessage.id).where(AIMessage.source_hash==digest)):continue
                timestamp=datetime.fromtimestamp(int(event.get('timestamp',0)),timezone.utc)
                if timestamp>now()+timedelta(minutes=5) or timestamp<now()-timedelta(days=7):continue
                identity=await channel_identity(session,settings,'whatsapp',subject,seen_at=timestamp)
                # timestamp # Customer-service window follows the signed event time, not a replay time.
                conv=await conversation(session,settings,identity=identity)
                kind=event.get('type');body=event.get('text',{}).get('body','') if kind=='text' else 'Клиент прислал вложение: '+str(kind)
                if kind=='text' and body.strip().casefold() in ('привязать аккаунт','/link'):
                    from .identity import issue_challenge
                    token=await issue_challenge(session,settings,identity)
                    await append(session,settings,conv,'Подтвердите аккаунт через обычный вход на сайте. Ссылка действует 10 минут.',event_id,'system',[dict(type='private_link',label='Подтвердить аккаунт',encrypted_url=encrypt(settings,settings.web_base_url.rstrip('/')+'/ai?link='+token))])
                else:await append(session,settings,conv,body[:3500],event_id)
                if kind!='text':
                    await handoff(session,settings,conv,'media_requires_operator')
                    await append(session,settings,conv,'В этом AI-канале пока доступны текстовые сообщения. Опишите проблему, а файл отправьте через обычную поддержку на сайте.','media-notice:'+event_id,'system')
                accepted+=1
    return accepted

async def deliver_one(database,settings):
    async with database.sessions.begin() as s:
        from .customer_config import enabled as customer_enabled
        if not await enabled(s,settings) and not await customer_enabled(s,settings):return False
        # An expired sending lease has an unknown external outcome. Do not resend blindly.
        stalled=list(await s.scalars(select(AIOutbox).where(AIOutbox.status=='sending',AIOutbox.lease_until<now()).with_for_update(skip_locked=True)))
        for item in stalled:item.status='uncertain';item.error_code='delivery_outcome_unknown'
        job=await s.scalar(select(AIOutbox).where(AIOutbox.status=='pending', (AIOutbox.lease_until.is_(None)) | (AIOutbox.lease_until<=now())).order_by(AIOutbox.id).with_for_update(skip_locked=True).limit(1))
        if not job:return False
        message=await s.get(AIMessage,job.message_id);conv=await s.get(AIConversation,message.conversation_id)
        identity=await s.get(ChannelIdentity,conv.identity_id) if conv.identity_id else None
        if not identity:job.status='failed';job.error_code='identity_missing';return True
        if not await channel_enabled(s,settings,identity.channel):job.status='canceled';job.error_code='channel_disabled';return True
        # A newer customer turn/operator takeover invalidates unsent model answers.
        if message.role=='assistant' and (conv.mode!='ai' or message.generation!=conv.generation):job.status='canceled';return True
        if identity.channel=='whatsapp' and now()-aware(identity.last_seen_at)>=timedelta(hours=24):job.status='requires_template';job.error_code='service_window_closed';return True
        job.status='sending';job.attempts+=1;job.lease_until=now()+timedelta(minutes=2)
        jid=job.id;attempt=job.attempts;subject=decrypt(settings,identity.encrypted_subject);channel=identity.channel
        text=message.text
        rich_buttons=[]
        actions=json.loads(message.actions)
        v21_reply=any(action.get('type')=='agent_version' for action in actions)
        for action in actions:
            if action.get('type')=='link':
                button=telegram_link_button(action) if channel=='telegram' else None
                if button:
                    rich_buttons.append(button)
                else:
                    text+='\n'+action['label']+': '+action['url']
            elif action.get('type')=='private_link':text+='\n'+action['label']+': '+decrypt(settings,action['encrypted_url'])
    from .v21.telegram import DeliveryRejected, RateLimited
    status='sent';code='';retry_at=None
    try:
        if channel=='telegram' and v21_reply:
            from .v21.telegram import send_reply
            await send_reply(database.sessions,settings,settings.bot_token,int(subject),message.text,actions)
        elif channel=='telegram':
            from app.web.support_rich import guide_html
            url='https://api.telegram.org/bot'+settings.bot_token+'/sendRichMessage'
            payload={'chat_id':int(subject),'rich_message':{'html':guide_html(text,{},False)+''.join(rich_buttons)}}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as client:
                async with client.post(url,json=payload,allow_redirects=False) as response:
                    if response.status>=500:
                        status='uncertain';code='delivery_outcome_unknown'
                    elif response.status!=200 or not (await response.json()).get('ok'):
                        status='failed';code='telegram_rich_rejected'
        elif channel=='whatsapp':
            if not (settings.freeman_ai_whatsapp_token and re.fullmatch(r'v\d+\.\d+',settings.freeman_ai_meta_api_version) and settings.freeman_ai_whatsapp_phone_id.isdigit()):raise ValueError('channel_not_configured')
            url=f'https://graph.facebook.com/{settings.freeman_ai_meta_api_version}/{settings.freeman_ai_whatsapp_phone_id}/messages'
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as client:
                async with client.post(url,json={'messaging_product':'whatsapp','to':subject,'type':'text','text':{'body':text[:4096]}},headers={'Authorization':'Bearer '+settings.freeman_ai_whatsapp_token},allow_redirects=False) as response:
                    if response.status not in (200,201):status='failed';code='channel_http_'+str(response.status)
        else:status='failed';code='unsupported_channel'
    except RateLimited as error:
        status='pending' if attempt<3 else 'failed';code='telegram_http_429'
        if status=='pending':retry_at=now()+timedelta(seconds=max(1,error.delay))
    except DeliveryRejected as error:status='failed';code='telegram_http_'+str(error.status)
    except (TimeoutError,aiohttp.ClientError):status='uncertain';code='delivery_outcome_unknown'
    except Exception:status='failed';code='channel_send_failed'
    async with database.sessions.begin() as s:
        job=await s.get(AIOutbox,jid);job.status=status;job.error_code=code;job.lease_until=retry_at
        s.add(AIEvent(conversation_id=conv.id,kind='delivery',name=channel,status=status,details=json.dumps({'error_code':code})))
    return True
