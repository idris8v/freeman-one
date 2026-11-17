"""Owner-authenticated, read-only SSE; durable jobs remain owned by the worker."""
import asyncio
import json
import time
from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from app.database.models import AIConversation, AIMessage, AIJob

def available():
    try:
        from app.services.freeman_ai.streaming import drafts
        from app.services.freeman_ai.provider import ResponsesProvider
        return callable(getattr(ResponsesProvider, 'complete_stream', None))
    except ImportError:
        return False

async def live_response(database, request, cid, uid, authorize):
    async with database.sessions() as session:
        c = await session.get(AIConversation,cid)
        if not c or c.user_id != uid or c.channel != 'owner' or c.mode == 'deleted':
            raise HTTPException(404, 'Чат недоступен.')
    if not available():
        return Response(status_code=204,headers={'Cache-Control':'no-store'})
    from app.services.freeman_ai.streaming import drafts
    async def events():
        started=time.monotonic();checked=0;job=None;last=None
        while time.monotonic()-started < 25:
            if await request.is_disconnected():return
            clock=time.monotonic()
            if clock-checked >= 1:
                try:
                    user=await authorize(request,owner=True)
                    if user.id != uid:return
                except HTTPException:return
                async with database.sessions() as s:
                    c=await s.get(AIConversation,cid)
                    if not c or c.user_id!=uid or c.channel!='owner' or c.mode!='ai':return
                    job=await s.scalar(select(AIJob).join(AIMessage,AIMessage.id==AIJob.message_id)
                        .where(AIMessage.conversation_id==cid).order_by(AIMessage.id.desc()).limit(1))
                    settled=not job or job.generation!=c.generation or job.status not in ('pending','processing')
                # The database session is closed before any network yield/sleep.
                if settled:
                    yield 'event: settled\ndata: {}\n\n'
                    return
                checked=clock
            item=drafts.read(job.id,job.lease) if job.status=='processing' else None
            if item:
                data=json.dumps(item,ensure_ascii=False)
                if data!=last:
                    yield 'event: draft\ndata: '+data+'\n\n';last=data
            else:
                yield ': waiting\n\n'
            await asyncio.sleep(.15)
        yield 'event: reconnect\ndata: {}\n\n'
    return StreamingResponse(events(),media_type='text/event-stream',headers={
        'Cache-Control':'no-store','X-Accel-Buffering':'no','X-Content-Type-Options':'nosniff'})
