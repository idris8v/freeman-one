"""Early callback feedback, safe navigation coalescing and payload-free timings."""
import asyncio,itertools,logging,re,time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass,field
from aiogram import BaseMiddleware
from sqlalchemy import event

current=ContextVar('bot_latency',default=None)
_sequence=itertools.count(1)
_latest={}
logger=logging.getLogger(__name__)
WATCH_SECONDS = (2, 8, 30)


def route_hint(update):
    """Fixed screen families only; no IDs, callback tokens or message text."""
    if not update.callback_query:
        text = getattr(update.message, 'text', None) or ''
        return 'start' if text.split(' ', 1)[0].split('@')[0] == '/start' else 'message'
    action = update.callback_query.data or ''
    first = action.split(':', 1)[0].split('~', 1)[0]
    if first == 'admin':
        second = action.split(':')[1] if ':' in action else ''
        return 'admin:'+second if second in {'home','r','p','t','x','input','clients','client','search','grantconfig','proxygrant','clientgrant'} else 'admin'
    return first if first in {'home','buy','plans','subscriptions','balance','referral','help','support','payment_management','p','g','vd','v','vr','sub','keydetail','devices','buy_devices','catalog_groups','tariff','renew','receipt','checkout','checkout_view','checkout_check','admin_subscription'} else 'other'

def view(action):
    # Financial actions, confirmations, refreshes, edits and FSM inputs always run.
    return action in ('home','buy','plans','subscriptions','balance','referral','help','support','payment_management','admin:home') or bool(re.fullmatch(r'(?:subscriptions:\d+|sub:\d+|devices:\d+|buy_devices:\d+(?::\d+)?|catalog_groups:\d+:\d+|vd:(?:list:\d+:\d+(?::refresh)?|view:\d+:[0-9a-f]{20})|admin:r:(?:key|devices|device|qr):[\d:a-f]+(?:~(?:s\d+|l\d+\.\d+))?)',action))

@dataclass
class Trace:
    seq:int
    kind:str
    key:object=None
    readonly:bool=False
    started:float=field(default_factory=time.perf_counter)
    wait_ms:float=0
    render_ms:float=0
    sql_ms:float=0
    queries:int=0
    api_ms:float=0
    api_calls:int=0
    render_depth:int=0
    superseded:bool=False
    route:str='other'
    pending_api:dict=field(default_factory=dict)


async def watch(trace):
    previous = 0
    for threshold in WATCH_SECONDS:
        await asyncio.sleep(threshold-previous)
        previous = threshold
        blocking_api = any(name != 'answerCallbackQuery' for name in trace.pending_api)
        stage = 'telegram' if blocking_api else 'render' if trace.render_depth else 'fsm_wait' if not trace.wait_ms else 'handler'
        logger.warning('Bot pending seq=%s kind=%s route=%s stage=%s elapsed_ms=%.1f methods=%s',
            trace.seq,trace.kind,trace.route,stage,(time.perf_counter()-trace.started)*1000,
            ','.join(sorted(trace.pending_api)) or '-')

def obsolete():
    trace=current.get()
    if trace and trace.readonly and trace.key is not None and _latest.get(trace.key)!=trace.seq:
        trace.superseded=True;return True
    return False

@contextmanager
def rendering():
    trace=current.get();start=time.perf_counter()
    if trace:trace.render_depth+=1
    try:yield
    finally:
        if trace:
            trace.render_depth-=1
            if not trace.render_depth:trace.render_ms+=(time.perf_counter()-start)*1000

def install_sql(engine):
    if getattr(engine.sync_engine,'_bot_timing_installed',False):return
    engine.sync_engine._bot_timing_installed=True
    @event.listens_for(engine.sync_engine,'before_cursor_execute')
    def start(conn,cursor,statement,parameters,context,many):
        context.bot_started=time.perf_counter() if current.get() else None
    @event.listens_for(engine.sync_engine,'after_cursor_execute')
    def finish(conn,cursor,statement,parameters,context,many):
        trace=current.get()
        if trace and context.bot_started is not None:
            trace.queries+=1;trace.sql_ms+=(time.perf_counter()-context.bot_started)*1000

class EarlyFeedback(BaseMiddleware):
    async def __call__(self,handler,update,data):
        query=update.callback_query
        key=None;action=query.data or '' if query else ''
        if query and query.message and query.message.chat.type=='private':key=(data['bot'].id,query.message.chat.id,query.from_user.id)
        trace=Trace(next(_sequence),'callback' if query else 'message',key,view(action));token=current.set(trace)
        trace.route=route_hint(update)
        watcher=asyncio.create_task(watch(trace))
        if key is not None:_latest[key]=trace.seq
        if query:
            from app.bot.handlers.navigation import start_callback_ack
            start_callback_ack(query.as_(data['bot']));data['callback_ack_started']=True
        try:return await handler(update,data)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher,return_exceptions=True)
            total=(time.perf_counter()-trace.started)*1000
            if total>=250 or trace.seq<=20 or trace.seq%20==0:
                logger.info('Bot timing seq=%s kind=%s total_ms=%.1f wait_ms=%.1f render_ms=%.1f sql_ms=%.1f queries=%s telegram_ms=%.1f calls=%s superseded=%s route=%s',trace.seq,trace.kind,total,trace.wait_ms,trace.render_ms,trace.sql_ms,trace.queries,trace.api_ms,trace.api_calls,trace.superseded,trace.route)
            if key is not None and _latest.get(key)==trace.seq:_latest.pop(key,None)
            current.reset(token)
