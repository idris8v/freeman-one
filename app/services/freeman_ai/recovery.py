"""Bounded recovery policy. Telegram send ambiguity must never trigger resends."""
import asyncio
import re
from contextlib import asynccontextmanager
import aiohttp
from sqlalchemy import text

FALLBACK = '💬 Передаю ваш вопрос специалисту. Он увидит переписку и продолжит отсюда.'

_SAFE_CODES = {
    'model_incomplete','model_not_configured','invalid_provider_url','model_invalid_json',
    'empty_model_reply','invalid_tool_contract','tool_budget_exhausted','too_many_tool_calls',
    'context_budget_exceeded','daily_token_budget_exceeded',
}

def failure_code(error):
    from app.services.freeman_ai.provider import ProviderUnavailable
    from app.services.freeman_ai.v21.telegram import DeliveryRejected, RateLimited
    if isinstance(error,DeliveryRejected):
        return 'telegram_http_'+str(error.status)
    if isinstance(error,RateLimited):
        return 'telegram_http_429'
    if isinstance(error,ProviderUnavailable):
        code=str(error)
        if re.fullmatch(r'model_http_[1-5][0-9]{2}',code) or code in _SAFE_CODES:
            return code
    if isinstance(error,ValueError) and str(error) in _SAFE_CODES:
        return str(error)
    if isinstance(error,TimeoutError):
        return 'timeout'
    return type(error).__name__[:60]

def transient(error):
    from app.services.freeman_ai.provider import ProviderUnavailable
    if isinstance(error,(TimeoutError,aiohttp.ClientError)):
        return True
    code=failure_code(error)
    return isinstance(error,ProviderUnavailable) and (
        code.startswith('model_http_5') or code in ('model_http_408','model_http_429','model_invalid_json','model_incomplete')
    )

def retry_generation(error, attempts, elapsed):
    # A second attempt helps quick upstream failures. After a long wait, return the recovery reply promptly.
    return attempts < 2 and elapsed < 15 and transient(error)


async def fallback(session,settings,conversation,*,reason,user_text,state=None):
    from .v21.backend import Backend
    from .v21.state import StateStore
    from .v21.intents import financial_risk, operator_requested
    from .v21.support_content import quick_diagnostic
    store=StateStore(session,settings)
    state=state or await store.load(conversation)
    has_financial_risk = financial_risk(user_text)
    if not state.financial_request and has_financial_risk:
        state.financial_request='Нужна проверка статуса платежа'
    delivery_failed = reason.startswith('telegram_') and reason != 'telegram_generation_exhausted'
    if not delivery_failed and not has_financial_risk and not operator_requested(user_text):
        diagnostic = quick_diagnostic(state, user_text)
        if diagnostic:
            diagnostic.remember(state)
            state.generation = conversation.generation
            await store.save(conversation, state, conversation.generation)
            return diagnostic.text, [dict(type='agent_version', version='2.1'), *diagnostic.actions]
    backend=Backend(session,settings,conversation,[])
    await backend.handoff({'reason':reason,'summary':'Сообщение сохранено в переписке. Нужна проверка оператором.'},state)
    state.generation=conversation.generation
    await store.save(conversation,state,conversation.generation)
    text=FALLBACK
    if state.financial_request:
        text='💳 Передаю вопрос по оплате специалисту. Если деньги уже списались, повторно оплачивать не нужно.'
    return text,[dict(type='agent_version',version='2.1')]

@asynccontextmanager
async def delivery_guard(database, lock, message_id):
    async with lock:
        if database.engine.dialect.name!='postgresql':
            yield True
            return
        async with database.engine.connect() as connection:
            await connection.execution_options(isolation_level='AUTOCOMMIT')
            key='support-recovery:'+str(message_id)
            claimed=bool((await connection.execute(text('SELECT pg_try_advisory_lock(hashtextextended(:key, 0))'),{'key':key})).scalar())
            try:yield claimed
            finally:
                if claimed:await connection.execute(text('SELECT pg_advisory_unlock(hashtextextended(:key, 0))'),{'key':key})
