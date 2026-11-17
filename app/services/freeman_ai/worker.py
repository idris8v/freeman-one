import asyncio,secrets,logging
from time import monotonic
from datetime import timedelta
from sqlalchemy import select,or_,update
from app.database.models import AIJob,AIMessage,AIConversation,AIEvent,now
from .service import enabled,append,handoff
from .provider import ResponsesProvider

async def process_one(database,settings,provider=None):
    token=secrets.token_hex(16)
    async with database.sessions.begin() as s:
        from .customer_config import enabled as customer_enabled
        if not await enabled(s,settings) and not await customer_enabled(s,settings):return False
        job=await s.scalar(select(AIJob).where(or_((AIJob.status=='pending')&or_(AIJob.lease_until==None,AIJob.lease_until<=now()),(AIJob.status=='processing')&(AIJob.lease_until<now()))).order_by(AIJob.id).with_for_update(skip_locked=True).limit(1))
        if not job:return False
        job.status='processing';job.lease=token;job.lease_until=now()+timedelta(minutes=10);job.attempts+=1
        jid=job.id;mid=job.message_id;generation=job.generation
    started=monotonic()
    from .streaming import drafts
    # Do not hold the conversation row lock across a network request. Takeover can proceed immediately.
    try:
        async with database.sessions.begin() as s:
            msg=await s.get(AIMessage,mid);conv=await s.get(AIConversation,msg.conversation_id)
            from .service import channel_enabled
            if not await channel_enabled(s,settings,conv.channel) or conv.mode!='ai' or conv.generation!=generation:
                await s.execute(update(AIJob).where(AIJob.id==jid,AIJob.lease==token).values(status='superseded'));return True
            history_filter=AIMessage.conversation_id==conv.id
            if conv.user_id and conv.channel!='owner':
                if conv.identity_id:
                    from app.database.models import ChannelIdentity
                    from .identity import customer
                    verified=await customer(s,await s.get(ChannelIdentity,conv.identity_id))
                    if not verified or verified.id!=conv.user_id:raise PermissionError('Account verification revoked')
            history=list(await s.scalars(select(AIMessage).where(history_filter).order_by(AIMessage.id.desc()).limit(32)))
            from .engine import run
            # Keep tool writes provisional until the response generation is still current.
            tool_transaction=await s.begin_nested()
            async def progress(text,phase):
                # Provisional, owner-only, no database writes or executable actions.
                drafts.publish(jid,token,mid,text,phase)
            from .customer_config import CustomerSettings
            runtime=settings if conv.channel=='owner' else CustomerSettings(settings)
            s.info["agent_sessions"]=database.sessions
            text,actions,requested_handoff=await run(s,runtime,conv,list(reversed(history)),provider or ResponsesProvider(runtime),progress=progress if conv.channel=='owner' else None)
            if requested_handoff:
                j=await s.get(AIJob,jid,populate_existing=True)
                if j.lease!=token or j.status!='processing':return True
                if conv.mode!='human' or conv.generation!=generation+1:j.status='superseded';return True
                await append(s,settings,conv,text,'handoff:'+str(jid),'system')
                j=await s.get(AIJob,jid);j.status='done';return True
            # Use the same lock order as owner sends/scheduler before the row lock.
            if conv.channel=='owner':
                from app.services.customer_identity import lock_nonce
                await lock_nonce(s,'ai:scheduled-conversation:'+str(conv.id))
            from .service import lock_ticket
            await lock_ticket(s,conv)
            # Recheck after model execution using a row lock and refreshed committed state.
            await s.execute(update(AIConversation).where(AIConversation.id==conv.id).values(updated_at=AIConversation.updated_at));await s.refresh(conv)
            j=await s.get(AIJob,jid,populate_existing=True)
            if j.lease!=token or j.status!='processing':
                await tool_transaction.rollback();return True
            if conv.mode!='ai' or conv.generation!=generation or not await channel_enabled(s,settings,conv.channel):
                await tool_transaction.rollback()
                await s.execute(update(AIJob).where(AIJob.id==jid,AIJob.lease==token).values(status='superseded'));return True
            await append(s,settings,conv,text,'response:'+str(jid),'assistant',actions);j.status='done'
    except Exception as error:
        # Neither provider response bodies nor customer text are logged.
        from app.services.freeman_ai.recovery import failure_code
        error_code=failure_code(error)
        logging.getLogger(__name__).warning('Freeman AI job failed: %s elapsed_ms=%d',error_code,(monotonic()-started)*1000)
        async with database.sessions.begin() as s:
            j=await s.get(AIJob,jid)
            if j and j.lease==token and j.status=='processing':
                j.status='failed';j.error_code=error_code
                msg=await s.get(AIMessage,mid);conv=await s.get(AIConversation,msg.conversation_id)
                if conv.channel=='owner':
                    from app.services.customer_identity import lock_nonce
                    await lock_nonce(s,'ai:scheduled-conversation:'+str(conv.id))
                from .service import lock_ticket
                await lock_ticket(s,conv)
                await s.execute(update(AIConversation).where(AIConversation.id==conv.id).values(updated_at=AIConversation.updated_at));await s.refresh(conv)
                if conv.mode=='ai' and conv.generation==generation:
                    if conv.channel=='owner':
                        reason='На сегодня исчерпан установленный лимит AI. Изменения не применены. Лимит обновится в 03:00 по Москве.' if str(error)=='daily_token_budget_exceeded' else 'Не удалось завершить запрос. Изменения не применены. Попробуй ещё раз.'
                        await append(s,settings,conv,reason,'failure:'+str(jid),'system')
                    else:
                        from app.services.freeman_ai.recovery import retry_generation, fallback
                        if retry_generation(error,j.attempts,monotonic()-started):
                            j.status='pending';j.lease_until=now()+timedelta(seconds=2)
                        else:
                            # A provider failure is not an operator takeover.
                            reply,actions=await fallback(s,settings,conv,reason=error_code,user_text=msg.text)
                            await append(s,settings,conv,reply,'failure:'+str(jid),'assistant',actions)
                s.add(AIEvent(conversation_id=conv.id,kind='failure',name=j.error_code,status='failed'))
    finally:
        drafts.discard(jid,token)
    return True

async def worker(database,settings):
    async def jobs():
        cleaned_at=None
        while True:
            try:
                if cleaned_at is None or now()-cleaned_at>timedelta(hours=1):
                    from .limits import cleanup
                    async with database.sessions.begin() as s:await cleanup(s,settings)
                    cleaned_at=now()
                from .tasks import tick
                await tick(database,settings)
                if not await process_one(database,settings):await asyncio.sleep(2)
            except asyncio.CancelledError:raise
            except Exception:
                logging.getLogger(__name__).warning('Freeman AI worker unavailable')
                await asyncio.sleep(5)
    async def outbox():
        from .channels import deliver_one
        while True:
            try:
                if not await deliver_one(database,settings):await asyncio.sleep(1)
            except asyncio.CancelledError:raise
            except Exception:
                logging.getLogger(__name__).warning('Freeman AI delivery unavailable')
                await asyncio.sleep(5)
    # Human replies must not wait behind a slow model request.
    await asyncio.gather(jobs(),outbox())
