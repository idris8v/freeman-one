"""Private HTMLBubbles purchase flow.

Queries share the existing single polling owner. Message bindings, actor checks
and frozen intents protect the existing real-payment handoff.
"""
import json
import logging
import re
from pathlib import Path
import zlib

import aiohttp

from app.services.admin_client_grant import rich

logger = logging.getLogger(__name__)


def widget_rejection(data):
    if not isinstance(data, dict) or not isinstance(data.get('description'), str):
        return 'unknown'
    description = data['description']
    code = re.search(r'\b(?:INPUT_MEDIA_WIDGET|WIDGET)_[A-Z_]{3,64}\b', description)
    if code:
        return code.group()
    reason = description.lower()
    for needle in ('chat not found', 'not supported', 'not available', 'disabled',
                   'chat type', 'too large', 'width', 'height', 'caption', 'reply markup',
                   'button', 'html', 'blocked', 'forbidden'):
        if needle in reason:
            return needle
    return 'unknown'


def widget_html(plans, extra, payment=None, *, antig=True, admin=False, recipient=None):
    if type(admin) is not bool:raise ValueError('Неверный режим карточки.')
    if type(antig) is not bool:raise ValueError('Неверная настройка антиглушилки.')
    if type(extra) is not int or not 0 <= extra <= 100000000:
        raise ValueError('Некорректная доплата.')
    if not plans or len(plans)>25 or any(len(p) not in (3,4,5) or any(type(n) is not int for n in p)
        or (len(p)==5 and not 0<=p[4]<=100000000) or not 1<=p[0]<=10000 or not 1<=p[1]<=120 or not 0<=p[2]<=100000000 for p in plans):
        raise ValueError('Нет подходящих тарифов.')
    pay=''
    if payment is not None:
        import re
        if not re.fullmatch(r'[A-Za-z0-9_]{5,32}',payment.get('bot','')) or not re.fullmatch(r'[0-9a-f]{32}',payment.get('token','')) or any(len(p)<4 or p[3]<1 for p in plans):
            raise ValueError('Неверная настройка оплаты виджета.')
        pay=True
    value=Path(__file__).with_name('widget_admin.html' if admin else 'widget_demo.html').read_text(encoding='utf-8')
    data=dict(plans=plans,pay=pay,antig=antig)
    if recipient:
        data['recipient']=recipient
    if any(len(p)<5 for p in plans):data['extra']=extra
    value=value.replace('__DEMO_DATA__',json.dumps(data,separators=(',',':')).replace('<','\\u003c'))
    raw=value.encode('utf-8')
    if len(raw)>32768 or len(zlib.compress(raw,9))>4096:
        raise ValueError('Карточка превышает допустимый размер виджета.')
    return value


async def catalogue(repo,user=None,*,admin=False):
    from app.services.bot_catalog import available
    from app.services.catalog import public_price
    from app.services.pricing import quote
    from app.services.bot_antig_price import monthly,tariff_total
    tariffs=await available(repo)
    plans={}
    for t in tariffs:
        if t.device_limit not in (1,3,5) or t.months not in (1,3,6,12):continue
        price=(await quote(repo,user.id,t,0))[0] if user and not admin else await public_price(repo,'vpn',t.id,t.price,False)
        k=(t.device_limit,t.months)
        plans[k]=min(plans.get(k,(price,t.id)),(price,t.id))
    result=[]
    for (d,m),(p,tid) in sorted(plans.items()):
        t=next(t for t in tariffs if t.id==tid)
        result.append([d,m,p,tid,await tariff_total(repo,t)] if user else [d,m,p])
    return result,await monthly(repo)


async def send_widget(settings, chat_id, html, *, payments=False, rows=None):
    """No implicit retry: an ambiguous send must not duplicate messages."""
    if type(chat_id) is not int or chat_id<=0:
        raise PermissionError('Доступно только в личном чате.')
    body=dict(chat_id=chat_id,html=html,allow_queries=payments,is_shared=False,disable_notification=True)
    if payments:
        body['caption']='Если виджет не загрузился, нажмите «Выбрать тариф кнопками» ниже.'
        body['reply_markup']={'inline_keyboard':rows or [[{'text':'← Назад','callback_data':'widget_back'}]]}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8),cookie_jar=aiohttp.DummyCookieJar()) as client:
            async with client.post('https://api.telegram.org/bot'+settings.bot_token+'/sendWidget',json=body,allow_redirects=False) as response:
                status=response.status
                data=await response.json(content_type=None)
    except (aiohttp.ClientError,TimeoutError,ValueError) as error:
        logger.warning('Widget send was not confirmed: %s', type(error).__name__)
        raise ValueError('Telegram не подтвердил отправку виджета. Проверьте чат; автоматического повтора нет.') from None
    if not isinstance(data,dict) or data.get('ok') is not True:
        logger.warning('Widget send rejected: HTTP %s, reason=%s', status, widget_rejection(data))
        if status in (404,403):
            raise ValueError('Telegram пока не разрешает отправку HTMLBubbles этому боту.')
        raise ValueError('Telegram отклонил карточку. Откройте покупку заново.')
    result=data.get('result',{})
    widget = result.get('widget') if isinstance(result,dict) else None
    if not isinstance(widget,dict) or type(result.get('message_id')) is not int:
        logger.warning('Telegram response omitted the widget or message ID')
        raise ValueError('Ответ Telegram не подтвердил отправку именно HTMLBubbles. Проверьте чат.')
    returned_html = widget.get('html')
    if not isinstance(returned_html,str) or not returned_html.strip():
        logger.warning('Telegram accepted the widget message but omitted its HTML')
        raise ValueError('Telegram не вернул HTML виджета.')
    logger.info('Telegram accepted widget: HTML matched=%s, bytes=%s', returned_html == html, len(returned_html.encode()))
    return result['message_id']


async def render(repo,settings,action,actor):
    if not settings.is_owner(actor.telegram_id):
        raise PermissionError('Раздел доступен только администратору.')
    if action=='admin:widgetdemo':
        return rich('🛒 <b>Оформление подписки</b>\n\nВыберите устройства и срок. Выдайте с учётом продажи или бесплатно; свои параметры можно указать отдельно.',
            [[('Открыть оформление','admin:widgetdemo:send')],[('← Назад','admin:commerce')]])
    if action!='admin:widgetdemo:send':raise ValueError('Действие недоступно.')
    await open_purchase(repo,settings,actor)
    return rich('Карточка подписки открыта ниже.',[[('← Продажи и тарифы','admin:commerce')]])


async def open_purchase(repo,settings,actor,*,client_id=None,client_origin=''):
    if actor.status=='blocked':raise PermissionError('Карточка недоступна.')
    admin=settings.is_owner(actor.telegram_id)
    recipient=None
    if client_id is not None:
        from app.services.admin_client_grant import ClientGrant
        from app.services.accounts import display_freeman_id
        customer=await ClientGrant(repo,settings).customer(client_id,actor)
        recipient=display_freeman_id(customer.freeman_id) or f'FO-{customer.id}'
    plans,extra=await catalogue(repo,actor,admin=admin)
    rows=[]
    from app.services.trial_offer import eligible as trial_eligible
    if await trial_eligible(repo,settings,actor):
        rows.append([dict(text='🎁 Попробовать бесплатно',callback_data='trial')])
    from app.services.admin_key_navigation import route
    fallback=route(f'admin:issue:{client_id}:list',client_origin) if client_id else 'widget_catalog'
    rows.append([dict(text='Выбрать тариф кнопками',callback_data=fallback)])
    rows.append([dict(text='← Назад',callback_data='widget_back')])
    from app.services.bot_widget_checkout import create_intent,key
    token=await create_intent(repo,settings,actor,plans,extra,admin=admin)
    snapshot=json.loads(await repo.setting(key(token)))
    if client_id is not None:
        from app.services.bot_antig_price import enabled
        snapshot.update(client_id=client_id, client_origin=client_origin, antig=await enabled(repo))
        await repo.set_setting(key(token),json.dumps(snapshot))
    mid=await send_widget(settings,actor.telegram_id,widget_html(plans,extra,dict(bot=settings.bot_username,token=token),antig=snapshot['antig'],admin=admin,recipient=recipient),payments=True,rows=rows)
    await repo.set_setting(f'widget_message:{actor.telegram_id}:{mid}',token)
    return mid
