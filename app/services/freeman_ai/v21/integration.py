"""Single v2.1 execution path for website tickets and Telegram Business."""
import json
import asyncio
import logging
import time
from sqlalchemy import select
from app.database.models import AIConversation,AIEvent
from app.database.repositories import SQLRepository
from app.services.freeman_ai.security import URL, chat_text
from .runtime import Agent
from .state import StateStore
from .backend import Backend

class Superseded(ValueError):pass

async def active(session):
    return await SQLRepository(session).setting('agent_v21_active','0')=='1'

LOG = logging.getLogger(__name__)


def budget_input(items):
    return [dict(item,encrypted_content='R'*min(4096,len(item['encrypted_content'])))
            if item.get('type')=='reasoning' and isinstance(item.get('encrypted_content'),str)
            else item for item in items]


def fit_model_input(items, instructions, schemas):
    """Keep the state, knowledge, latest client message and all tool exchanges."""
    from app.services.freeman_ai.limits import reservation_amount
    fitted=list(items)
    history_end=next((i for i,item in enumerate(fitted) if item.get('type')),len(fitted))
    latest_user=next((i for i in range(history_end-1,1,-1) if fitted[i].get('role')=='user'),None)
    if latest_user is None:
        return fitted
    system=dict(role='system',content=instructions)
    while latest_user>2 and reservation_amount([system]+budget_input(fitted),schemas)>65000:
        fitted.pop(2)
        latest_user-=1
    return fitted


class BudgetProvider:
    def __init__(self,session,settings,provider):
        self.session,self.settings,self.provider=session,settings,provider
        self.usage=0
        self.calls=0
        self.quick_reply_timeout=getattr(provider,'quick_reply_timeout',8)

    async def complete(self,items,instructions,schemas):
        from app.services.freeman_ai.limits import reserve,settle
        from app.services.freeman_ai.recovery import failure_code
        items=fit_model_input(items,instructions,schemas)
        reservation=await reserve(self.session,self.settings,[dict(role='system',content=instructions)]+budget_input(items),schemas,context_limit=65000)
        self.calls+=1
        started=time.monotonic()
        outcome='ok'
        try:
            data=await self.provider.complete(items,instructions,schemas)
        except asyncio.CancelledError:
            outcome='cancelled'
            raise
        except Exception as error:
            outcome=failure_code(error)
            raise
        finally:
            LOG.info('Customer AI provider call: sequence=%s elapsed_ms=%d outcome=%s',
                     self.calls,(time.monotonic()-started)*1000,outcome)
        total=int(data.get('usage',{}).get('total_tokens',reservation[1]))
        await settle(self.session,reservation,total);self.usage+=total
        return data

async def run(session,settings,conv,history,provider,*,sessions=None,verified_user=None):
    if not await active(session):raise ValueError('agent_v21_not_activated')
    if sessions is not None:session.info['agent_sessions']=sessions
    store=StateStore(session,settings);state=await store.load(conv)
    if conv.mode!='ai':raise Superseded('conversation_superseded')
    if state.phase=='operator_running':state.return_to_agent()
    state.generation=conv.generation;generation=conv.generation
    # Only the current conversation supplies key possession. Other conversations
    # may have shared account history, but cannot transfer capability by a quote.
    own_history=[m for m in history if m.conversation_id==conv.id]
    backend=Backend(session,settings,conv,own_history,verified_user=verified_user)
    state.verified_customer=backend.uid
    # Retain actual screenshots without importing any old diagnostic prompts.
    if conv.channel in ('support_agent_telegram','support_account_test','support_agent_website','support_agent_account'):
        from app.services.freeman_ai.images import model_images
        from app.database.models import now
        from app.services.freeman_ai.security import aware
        image_map={}
        for m in reversed(own_history):
            if m.role!='client' or (now()-aware(m.created_at)).total_seconds()>1800:continue
            pictures=await model_images(session,settings,m.id)
            if pictures:image_map[m.id]=[{**p,'detail':'high'} for p in pictures[:2]]
            if len(image_map)>=2:break
    else:
        from app.services.freeman_ai.support_vision import recent_ticket_images
        image_map=await recent_ticket_images(session,settings,conv,own_history)
    items=[]
    for m in own_history[-32:]:
        version=next((a.get('version') for a in json.loads(m.actions) if a.get('type')=='agent_version'),None)
        # The model gets only a marker. Just the trusted backend sees the
        # literal value to check possession and build copy and import actions.
        content=URL.sub(
            lambda match: '[клиент прислал ссылку подписки]' if match[0].startswith('https://sub.freemanone.top/') else match[0],
            chat_text(m.text),
        )
        if image_map.get(m.id):content=[dict(type='input_text',text=content)]+image_map[m.id]
        items.append(dict(role=m.role,content=content,knowledge_version=version))
    async def current(expected):
        result=await session.scalar(select(AIConversation.generation).where(AIConversation.id==conv.id,AIConversation.mode=='ai'))
        return result==expected
    from app.services.freeman_ai.provider import ResponsesProvider as LegacyProvider
    if isinstance(provider,LegacyProvider):
        from .provider import ResponsesProvider
        provider=ResponsesProvider(settings)
    budget=BudgetProvider(session,settings,provider)
    async with asyncio.timeout(60):
        reply=await Agent(budget,backend).turn(state,items,current)
    if reply is None:raise Superseded('conversation_superseded')
    await store.save(conv,state,generation)
    session.add(AIEvent(conversation_id=conv.id,kind='model.usage',name='agent_v21',details=json.dumps(dict(total_tokens=budget.usage,knowledge_version='2.1'))))
    return chat_text(reply.text),[dict(type='agent_version',version='2.1')]+reply.actions,False
