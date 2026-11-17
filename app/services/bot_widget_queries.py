"""Private customer HTMLBubbles requests on the existing polling owner.

Never accept capabilities, actors or payment IDs from widget JavaScript. A
server-side chat/message binding selects the intent, and every request refreshes
the staff role. The existing checkout owns pricing, verification and issuance.
"""
import asyncio
import json
import logging

import aiohttp
from sqlalchemy import select

from app.database.models import User, now
from app.database.repositories import SQLRepository

logger = logging.getLogger(__name__)


async def api(settings, method, body, *, timeout=8):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout),cookie_jar=aiohttp.DummyCookieJar()) as client:
        async with client.post('https://api.telegram.org/bot'+settings.bot_token+'/'+method,json=body,allow_redirects=False) as response:
            data=await response.json(content_type=None)
    if not isinstance(data,dict) or data.get('ok') is not True:
        # Provider descriptions/URLs can carry secrets: never echo them.
        raise ValueError('Telegram не подтвердил действие. Повторите позже.')
    return data.get('result')


async def poll(bot,method):
    """One consumer with the same offsets, filters and long-poll timeout."""
    body=method.model_dump(exclude_none=True)
    # Reuse aiogram's keep-alive pool, proxy and connector rather than establish
    # a new TLS connection for every polling request.
    client=await bot.session.create_session()
    async with client.post('https://api.telegram.org/bot'+bot.token+'/getUpdates',json=body,allow_redirects=False,timeout=aiohttp.ClientTimeout(total=30)) as response:
        value=await response.text()
        result=bot.session.check_response(bot,method,response.status,value)
    return result.result


async def dispatch(repo,settings,user,chat_id,message_id,request):
    """Return JSON plus an optional existing Screen; no Telegram writes here."""
    from app.services.bot_widget_checkout import key,create_intent,prepare
    from app.services.bot_checkout import BotCheckout
    token=await repo.setting(f'widget_message:{chat_id}:{message_id}')
    if not token:raise ValueError('Карточка устарела. Откройте «Купить подписку».')
    from app.services.offers import lock_customer
    await lock_customer(repo,user.id)
    root=json.loads(await repo.setting(key(token),'{}'))
    if chat_id!=user.telegram_id or root.get('actor')!=user.id or user.status=='blocked':
        raise PermissionError('Карточка недоступна.')
    if root.get('expires',0)<now().timestamp():
        root['expires']=now().timestamp()+3600
        await repo.set_setting(key(token),json.dumps(root))
    if not isinstance(request,str) or not 1<=len(request.encode())<=64:raise ValueError('Неверное действие.')
    from app.services.screens import ScreenService
    from app.services.integrations import MockPaymentService,MockProvisioningService
    service=ScreenService(repo,settings,MockPaymentService(),MockProvisioningService())
    import re
    admin_match=re.fullmatch(r'(issue|free|review|confirm):([1-9][0-9]{0,10}):([01])',request)
    if admin_match:
        if admin_match[1] in ('review','confirm'):
            from app.services import bot_widget_admin
            method=bot_widget_admin.review if admin_match[1]=='review' else bot_widget_admin.confirm
            return await method(repo,settings,user,token,root,int(admin_match[2]),int(admin_match[3]))
        from app.services.bot_widget_admin import prepare as admin_prepare
        return await admin_prepare(repo,settings,user,token,root,int(admin_match[2]),int(admin_match[3]),'sale' if admin_match[1]=='issue' else 'free')
    if request in ('custom:0','custom:1'):
        from app.services.bot_widget_admin import custom
        return await custom(service,user,root,int(request[-1]))
    if request=='back':
        root['stage']=0
        await repo.set_setting(key(token),json.dumps(root))
        return dict(reset=True),None
    if request in ('home','custom','gift'):
        if request=='custom' and not settings.is_owner(user.telegram_id):raise PermissionError('Действие недоступно.')
        action={'home':'home','custom':f'admin:grantconfig:new:{user.id}:buy','gift':'cg:hub'}[request]
        screen=await service.render(action,user)
        if request!='home' and screen.rows:screen.rows[-1]=[('← Назад','buy')]
        return {},screen
    import re
    match=re.fullmatch(r'(pay|wallet|check|status|receipt|grant):([1-9][0-9]{0,10}):([01])',request)
    if not match:raise ValueError('Неверное действие.')
    op,tid,anti=match[1],int(match[2]),int(match[3])
    if root.get('admin_mode') and op in ('pay','wallet'):
        raise PermissionError('Используйте администраторскую выдачу.')
    if not any(p[3]==tid for p in root.get('plans',[]) if len(p)>=4):raise ValueError('Тариф недоступен.')
    if anti and op in ('pay','wallet','grant') and root.get('antig') is False:
        raise ValueError('Откройте новую карточку для выбора антиглушилки.')
    if op=='grant':
        if not settings.is_owner(user.telegram_id):raise PermissionError('Действие недоступно.')
        from app.services.bot_antig_test import render
        screen=await render(service,f'antig_test:tariff:{tid}:'+('yes' if anti else 'no'),user)
        screen.rows[-1]=[('← Изменить тариф','buy')]
        return {},screen
    choice=f'{tid}:{anti}'
    children=root.setdefault('choices',{})
    child=children.get(choice)
    if op=='pay':
        if child:
            previous=json.loads(await repo.setting(key(child),'{}'))
            old=await BotCheckout(repo,settings).owned(previous.get('payment',0),user) if previous.get('payment') else None
            if old and old.status=='order_canceled':
                child=None  # A confirmed cancellation permits a new immutable order.
        if not child:
            child=await create_intent(repo,settings,user,root['plans'],root['extra'])
            children[choice]=child
            await repo.set_setting(key(token),json.dumps(root))
        screen=await prepare(repo,settings,user,child,tid,anti)
        intent=json.loads(await repo.setting(key(child),'{}'))
        if not intent.get('payment'):
            # Never silently accept a changed price or issue a mismatched invoice.
            raise ValueError('Цена изменилась. Откройте «Купить подписку» заново.')
        p=await BotCheckout(repo,settings).owned(intent['payment'],user)
        root['stage']=1
        await repo.set_setting(key(token),json.dumps(root))
        if p.status=='order_paid':return dict(paid=True),None
        url=next((t[4:] for row in screen.rows for _,t in row if t.startswith('url:https://')),None)
        if not url:raise ValueError('Счёт закрыт. Откройте «Купить подписку».')
        started=bool(p.provider_id or await repo.setting(f'bot_checkout_started:{p.id}'))
        return dict(url=url,balance=await repo.balance(user.id),locked=started,_payment=p.id),None
    if not child:raise ValueError('Сначала перейдите к оплате.')
    intent=json.loads(await repo.setting(key(child),'{}'))
    checkout=BotCheckout(repo,settings)
    p=await checkout.owned(intent.get('payment',0),user)
    if op=='wallet':
        from app.services.bot_widget_balance import settle
        await settle(checkout,p,user)
        return dict(paid=True),await checkout.service.success_screen(p,user)
    if op=='check' and p.provider_id and p.status!='order_paid':
        await checkout.service.credit(p,await checkout.service.provider.get(p.provider_id))
    if op=='status' and p.status not in ('order_paid','order_canceled'):
        marker=f'widget_bank_confirmed:{p.id}'
        if await repo.setting(marker)=='1':
            return dict(phase='confirmed',locked=True),None
        if p.provider_id:
            result=await checkout.service.provider.get(p.provider_id)
            if result.get('status')=='succeeded' and result.get('paid') is True:
                checkout.service.verify_confirmation(p,result,await checkout.service.details(p))
                await repo.set_setting(marker,'1')
                return dict(phase='confirmed',locked=True),None
            if result.get('status')=='canceled':
                await checkout.service.credit(p,result)
        if p.status!='order_canceled':
            return dict(phase='waiting',locked=bool(p.provider_id or await repo.setting(f'bot_checkout_started:{p.id}'))),None
    if op=='receipt':
        if p.status!='order_paid':raise ValueError('Оплата ещё не подтверждена.')
        return {},await checkout.service.success_screen(p,user)
    if p.status=='order_paid':return dict(paid=True),None
    balance=await repo.balance(user.id)
    if p.status=='order_canceled':
        return dict(canceled=True,balance=balance,note='Банк отменил платёж. Можно подготовить новый счёт или оплатить с баланса после нового расчёта.'),None
    started=bool(p.provider_id or await repo.setting(f'bot_checkout_started:{p.id}'))
    return dict(balance=balance,locked=started,note='Оплата ещё не подтверждена. Можно снова открыть тот же счёт. Если уже оплатили, дождитесь подтверждения.'),None


async def back(screens,user,query):
    """Native keyboard below the widget: checkout → selection → home."""
    from app.services.bot_widget_checkout import key
    from app.services.offers import lock_customer
    repo,settings=screens.repo,screens.settings
    if query.message.chat.type!='private' or query.message.chat.id!=user.telegram_id:
        raise PermissionError('Карточка недоступна.')
    await lock_customer(repo,user.id)
    token=await repo.setting(f'widget_message:{user.telegram_id}:{query.message.message_id}')
    root=json.loads(await repo.setting(key(token),'{}')) if token else {}
    if user.status=='blocked':raise PermissionError('Карточка недоступна.')
    if not root:
        return await back_home(screens,user,query)
    if root.get('actor')!=user.id:raise PermissionError('Карточка недоступна.')
    if root.get('stage')==1:
        import secrets
        await api(settings,'editMessageWidgetData',dict(chat_id=user.telegram_id,message_id=query.message.message_id,
            chat_data=json.dumps(dict(selection=secrets.token_hex(4)))))
        root['stage']=0
        await repo.set_setting(key(token),json.dumps(root))
        return
    client_id=root.get('client_id')
    from app.services.admin_key_navigation import route
    destination=route(f'admin:client:{client_id}',root.get('client_origin','')) if client_id else 'home'
    return await back_home(screens,user,query,destination)


async def back_home(screens,user,query,action='home'):
    if query.message.chat.type!='private' or query.message.chat.id!=user.telegram_id or user.status=='blocked':
        raise PermissionError('Карточка недоступна.')
    from app.bot.delivery import fresh_context
    from app.bot.handlers.navigation import show
    from aiogram.exceptions import TelegramBadRequest
    await show(fresh_context(query),await screens.render(action,user))
    try:await query.message.delete()
    except TelegramBadRequest:pass


async def handle(runtime,bot,query):
    """Unknown/new SDK fields never enter aiogram's legacy Update dispatcher."""
    qid=query.get('id') if isinstance(query,dict) else None
    if not qid:return
    logger.info('Widget request received')
    answer={};screen=None;cid=mid=uid=0
    try:
        sender=query.get('from',{})
        uid=sender.get('id');chat=query.get('chat',{})
        cid=chat.get('id');mid=query.get('message_id')
        if type(uid) is not int or cid!=uid or chat.get('type')!='private' or type(mid) is not int or mid<1:
            raise PermissionError('Доступно только в личном чате с ботом.')
        async with runtime.database.sessions() as session:
            async with session.begin():
                repo=SQLRepository(session)
                role=await repo.setting(f'staff_role:{uid}')
                blocked=await repo.setting(f'staff_blocked:{uid}')=='1'
                runtime.settings.set_staff_role(uid,None if blocked else role)
                user=await session.scalar(select(User).where(User.telegram_id==uid))
                if not user or user.status=='blocked':raise PermissionError('Доступ ограничен.')
                operation=query.get('data','').split(':')[0] if isinstance(query.get('data'),str) else 'invalid'
                logger.info('Widget operation: %s',operation if operation in ('pay','wallet','check','receipt','review','confirm','issue','free','custom','back') else 'other')
                answer,screen=await asyncio.wait_for(dispatch(repo,runtime.settings,user,cid,mid,query.get('data')),10)
        logger.info('Widget result: invoice=%s paid=%s review=%s',bool(answer.get('url')),bool(answer.get('paid')),bool(answer.get('review')))
        admin_grant=answer.pop('_admin_grant',None)
        if admin_grant:
            from app.services.bot_widget_admin import execute
            screen=await execute(runtime.database,runtime.settings,uid,admin_grant)
        answer.pop('_payment',None)
        # Commit an order before exposing its capability URL; a lost answer can
        # be retried safely against the same frozen child intent.
        if screen is not None:
            from app.bot.handlers.navigation import show
            from aiogram.types import Message,Chat,User as TelegramUser
            from datetime import datetime,timezone
            context=Message(message_id=mid,date=datetime.now(timezone.utc),chat=Chat(id=cid,type='private'),from_user=TelegramUser(id=uid,is_bot=False,first_name='')).as_(bot)
            if screen.admin_input_action:
                from app.bot.states import AdminInput
                state=runtime.dispatcher.fsm.get_context(bot=bot,chat_id=cid,user_id=uid)
                await state.set_state(AdminInput.waiting)
                await state.update_data(admin_action=screen.admin_input_action)
            await show(context,screen)
            await bot.delete_message(cid,mid)
    except (PermissionError,ValueError) as error:
        logger.warning('Widget request rejected: %s',type(error).__name__)
        answer={'error':str(error)}
    except Exception:
        # No query payload, URL or token in application logs.
        logger.warning('Widget request failed',exc_info=False)
        answer={'error':'Не удалось завершить действие. Повторите позже.'}
    try:
        await api(runtime.settings,'answerWidgetQuery',dict(widget_query_id=qid,data=json.dumps(answer,ensure_ascii=False,separators=(',',':'))))
    except Exception:
        logger.warning('Widget answer was not confirmed')


async def legacy_check(screens,user,query,pid):
    """Old native buttons update their own label; never show another Screen."""
    from app.services.bot_checkout import BotCheckout
    if query.message.chat.id!=user.telegram_id:raise PermissionError('Карточка недоступна.')
    checkout=BotCheckout(screens.repo,screens.settings)
    payment=await checkout.owned(pid,user)
    async def label(value):
        await api(screens.settings,'editMessageReplyMarkup',dict(chat_id=user.telegram_id,message_id=query.message.message_id,
            reply_markup=dict(inline_keyboard=[[dict(text=value,callback_data=f'checkout_check:{pid}')],
                [dict(text='← Назад',callback_data='widget_back')]])))
    await label('⏳ Проверяем…')
    try:
        if payment.provider_id and payment.status!='order_paid':
            await checkout.service.credit(payment,await checkout.service.provider.get(payment.provider_id))
        value='✅ Оплачено' if payment.status=='order_paid' else 'Не оплачено'
    except ValueError:value='Не удалось проверить · повторить'
    await label(value)
