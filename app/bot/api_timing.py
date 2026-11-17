"""Log slow Telegram requests without chat IDs, payloads or credentials."""
import logging
import asyncio
from time import perf_counter
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramNetworkError

EDIT_SECONDS = 8
MEDIA_EDIT_SECONDS = 30
EDITS = {'editMessageText', 'editMessageCaption', 'editMessageReplyMarkup', 'editMessageMedia'}


def edit_budget(method):
    if method.__api_method__ not in EDITS:
        return None
    rich = getattr(method, 'rich_message', None)
    media = rich and (rich.media or any(getattr(block, 'type', '') in {'photo','video','animation','audio','file','collage','slideshow'} for block in (rich.blocks or [])))
    return MEDIA_EDIT_SECONDS if method.__api_method__ == 'editMessageMedia' or media else EDIT_SECONDS


class ApiTiming(BaseRequestMiddleware):
    async def __call__(self,make_request,bot,method):
        started=perf_counter()
        from app.bot.latency import current
        trace=current.get()
        name=method.__api_method__
        if trace and name!='getUpdates':trace.pending_api[name]=trace.pending_api.get(name,0)+1
        try:
            budget = edit_budget(method)
            if budget is None:
                return await make_request(bot,method)
            try:
                return await asyncio.wait_for(make_request(bot,method), timeout=budget)
            except TimeoutError:
                # Never retry a mutation or resend a possibly accepted message.
                raise TelegramNetworkError(method=method, message='Navigation edit timed out; retry the button') from None
        finally:
            elapsed=perf_counter()-started
            if trace and name in trace.pending_api:
                trace.pending_api[name]-=1
                if not trace.pending_api[name]:trace.pending_api.pop(name)
            if trace and method.__api_method__!='getUpdates':trace.api_calls+=1;trace.api_ms+=elapsed*1000
            if elapsed>=1 and method.__api_method__!='getUpdates':
                logging.getLogger(__name__).warning('Slow Telegram request: %s %.2fs',method.__api_method__,elapsed)
