"""Private customer widget handoff. Existing YooKassa verification owns payment/issue."""
import hashlib
import json
import re
import secrets
import uuid

from app.database.models import AppSetting, Payment, now
from app.services.admin_client_grant import rich
from app.services.screens import money, duration


def key(token):
    if not re.fullmatch(r'[0-9a-f]{32}',token):raise ValueError('Ссылка виджета недействительна.')
    return 'widget_intent:'+hashlib.sha256(token.encode()).hexdigest()


async def button_checkout(repo, settings, user, parts):
    from app.services.bot_antig_price import require_enabled, tariff_total
    from app.services.bot_catalog import devices_label
    from app.services.pricing import quote

    if settings.role(user.telegram_id) or user.status != 'active' or len(parts) not in (2, 3):
        raise ValueError('Оформление недоступно.')
    tid = int(parts[1])
    tariff = await repo.tariff_by_id(tid)
    if (not tariff or await repo.tariff_setting(tariff,'tariff_enabled','1') != '1'
            or await repo.tariff_setting(tariff,'tariff_deleted','0') == '1'):
        raise ValueError('Тариф недоступен.')
    await require_enabled(repo)
    if len(parts) == 3:
        return await prepare(repo, settings, user, parts[2], tid, 1, buttons=True)
    base = (await quote(repo,user.id,tariff,0))[0]
    extra = await tariff_total(repo,tariff)
    token = await create_intent(repo,settings,user,[[tariff.device_limit,tariff.months,base,tid,extra]],extra)
    row = await repo.session.get(AppSetting,key(token))
    data = json.loads(row.value)
    data['button_checkout'] = True
    row.value = json.dumps(data)
    target = f'bantig:{tid}:{token}'
    from app.services.bot_antig_test import choice_screen
    result = choice_screen(f'{devices_label(tariff.device_limit)} · {duration(tariff.months)}',
                         extra,target,f'tariff:id{tid}',f'buy_devices:{tariff.device_limit}',base=base)

    if await repo.setting('payment_provider:stars', '0') == '1':
        from app.services.stars import MAX_INVOICE_STARS, bundle_stars
        total = await bundle_stars(repo, tariff, base, extra)
        if 1 <= total <= MAX_INVOICE_STARS:
            result.rows.insert(1, [(f'⭐ С антиглушилкой · {total} ⭐', f'stars:id{tid}:0:antig')])
    return result



async def create_intent(repo,settings,user,plans,extra,*,admin=False):
    if user.status=='blocked' or not user.telegram_id:raise PermissionError('Карточка недоступна.')
    if admin and not settings.is_owner(user.telegram_id):raise PermissionError('Администраторская выдача недоступна.')
    token=secrets.token_hex(16)
    from app.services.bot_antig_price import enabled
    await repo.set_setting(key(token),json.dumps(dict(actor=user.id,plans=plans,extra=extra,admin_mode=bool(admin),antig=True if admin else await enabled(repo),expires=now().timestamp()+3600)))
    return token


async def prepare(repo,settings,user,token,tid,anti,accepted=None,*,buttons=False):
    if user.status=='blocked' or not user.telegram_id:raise PermissionError('Карточка недоступна.')
    if type(tid) is not int or type(anti) is not int or anti not in (0,1):raise ValueError('Некорректный выбор.')
    from app.services.offers import lock_customer,reserve
    from app.services.bot_checkout import BotCheckout
    from app.services.payment_settings import PaymentSettings
    await PaymentSettings(repo,settings).hydrate()
    if settings.yookassa_test_mode:raise ValueError('Для оплаты нужна рабочая ЮKасса. Сейчас включена песочница.')
    checkout=BotCheckout(repo,settings)
    if not checkout.service.enabled or await repo.setting('payment_provider:yookassa','1')!='1':raise ValueError('ЮKасса сейчас недоступна.')
    await lock_customer(repo,user.id)
    row=await repo.session.get(AppSetting,key(token),with_for_update=True,populate_existing=True)
    data=json.loads(row.value) if row else {}
    if data.get('actor')!=user.id or user.status=='blocked':raise PermissionError('Эта карточка принадлежит другому пользователю.')
    if buttons and not data.get('button_checkout'):
        raise ValueError('Откройте выбор тарифа заново.')
    if data.get('payment'):
        payment=await checkout.owned(data['payment'],user)
        note='' if data['choice']==[tid,anti] else 'Для этой карточки уже создан счёт с другим выбором. Чтобы оформить другую подписку, откройте новый виджет.'
        return await payment_screen(checkout,payment,user,note=note)
    if data.get('expires',0)<now().timestamp():
        data['expires']=now().timestamp()+3600
        row.value=json.dumps(data)
    if anti:
        from app.services.bot_antig_price import require_enabled
        await require_enabled(repo)
        if data.get('antig') is False:raise ValueError('Откройте новую карточку для выбора антиглушилки.')
    shown=next((p for p in data.get('plans',[]) if len(p)>=4 and p[3]==tid),None)
    tariff=await checkout.lock_tariff(tid) if shown else None
    if not tariff or [tariff.device_limit,tariff.months]!=shown[:2] or await repo.tariff_setting(tariff,'tariff_enabled','1')!='1' or await repo.tariff_setting(tariff,'tariff_deleted','0')=='1':
        raise ValueError('Выбранный тариф больше недоступен. Откройте новый виджет.')
    from app.services.pricing import quote
    from app.services.bot_antig_price import tariff_total
    base=(await quote(repo,user.id,tariff,0))[0]
    extra=await tariff_total(repo,tariff) if anti else 0
    total=base+extra
    if type(total) is not int or not 100<=total<=100000000:raise ValueError('Сумма оплаты должна быть от 1 до 1 000 000 ₽.')
    displayed=shown[2]+((shown[4] if len(shown)>4 else data['extra']*shown[1]) if anti else 0)
    if total!=displayed and accepted!=total:
        target=f'wpay:{token}:{tid}:{anti}:{total}'
        result=rich('🔄 <b>Цена обновилась</b>\n\nВ карточке: '+money(displayed)+'\nСейчас: <b>'+money(total)+'</b>'
            +'\n\nПроверьте новую сумму перед переходом к реальной оплате.',[[('💳 Продолжить · '+money(total),target)],[('← Назад','buy')]])
        result.button_options[target]={'style':'success'}
        return result
    from app.services.bot_antig_test import resolve
    access=await resolve(repo,settings,bool(anti))
    from app.services.billing import purchase_limits
    devices,traffic=await purchase_limits(repo,tariff)
    payment=await repo.add(Payment(user_id=user.id,amount=total,status='order_created'))
    await reserve(repo,payment,tariff,0,base)
    order=dict(months=tariff.months,tariff_id=tid,sid=0,amount=total,total=total,balance_applied=0,
        payment_method='sbp',return_url='',devices=devices,traffic_bytes=traffic,
        bot_test_access=access,antig_surcharge=extra,antig_base_total=base,widget_intent=key(token))
    if data.get('button_checkout'):
        order['button_checkout'] = True
        await repo.set_setting(f'button_antig_order:{payment.id}','1')
    await repo.set_setting(f'order:{payment.id}',json.dumps(order))
    await repo.set_setting(f'order_idem:{payment.id}',str(uuid.uuid4()))
    await repo.set_setting(f'bot_checkout:{payment.id}','1')
    await repo.set_setting(f'widget_order:{payment.id}',key(token))
    data.update(payment=payment.id,choice=[tid,anti]);row.value=json.dumps(data)
    return await payment_screen(checkout,payment,user)


async def payment_screen(checkout,payment,user,note='',action=''):
    from app.services.bot_checkout import BANK_WAIT
    order=await checkout.service.details(payment)
    buttons=order.get('button_checkout')
    if action.startswith('checkout_balance:'):
        if buttons:
            from app.services.bot_widget_balance import settle
            async with checkout.repo.session.begin_nested():
                await settle(checkout,payment,user)
            return await checkout.service.success_screen(payment,user)
        raise ValueError('Оплата с баланса доступна в карточке подписки. С баланса ничего не списано.')
    if payment.status=='order_paid':return await checkout.service.success_screen(payment,user)
    if action.startswith('checkout_check:') and payment.provider_id:
        if await checkout.service.credit(payment,await checkout.service.provider.get(payment.provider_id)):
            return await checkout.service.success_screen(payment,user)
        note='Оплата пока не подтверждена. Если уже оплатили, повторно не платите.'
    if payment.status=='order_canceled':
        return rich('Счёт отменён. Откройте новую карточку для повторного выбора.',[[('← Выбор подписки','buy')]])
    result=await checkout.screen(payment,user)
    payment_row=next(row for row in result.rows if any(t.startswith('url:') for _,t in row))
    result.rows=[payment_row,[('🔄 Проверить оплату',f'checkout_check:{payment.id}')],[('← Выбор подписки','buy')]]
    if buttons:
        result.rows[-1]=[('← Изменить выбор',f'bantig:{order["tariff_id"]}')]
        if payment.status == 'order_created' and not await checkout.repo.setting(f'bot_checkout_started:{payment.id}') and await checkout.repo.balance(user.id)>=payment.amount:
            result.rows.insert(1,[('💰 Оплатить с баланса',f'checkout_balance:{payment.id}')])
    text_value=('💳 <b>Оплата подписки</b>\n\n'
        f'{order["devices"]} устр. · {duration(order["months"])}\n'
        '<b>Антиглушилка:</b> '+('Да' if len(order['bot_test_access']['squads'])==2 else 'Нет')
        +'\n\nПодписка: '+money(order['antig_base_total'])+'\nДоплата: '+money(order['antig_surcharge'])
        +'\n<b>К оплате: '+money(payment.amount)+'</b>\n\n'+BANK_WAIT+'\n\n✅ После подтверждения оплаты ключ появится в боте.'
        +('\n\n'+note if note else ''))
    from app.bot.rich_copy import text_blocks
    from aiogram.types import InputRichMessage
    result.text=text_value;result.rich=InputRichMessage(blocks=text_blocks(text_value),skip_entity_detection=True)
    result.button_options[payment_row[0][1]]={'style':'success'}
    return result


async def verify_paid_profile(repo,user,terms,operation_key):
    """Allow extra squads only from an authenticated, frozen widget invoice."""
    match=re.fullmatch(r'payment:([1-9][0-9]*)',str(operation_key or ''))
    if not match:raise ValueError('Платная выдача антиглушилки не подтверждена.')
    pid=int(match[1]);p=await repo.session.get(Payment,pid,populate_existing=True)
    if p and p.status == 'stars_processing':
        stored = json.loads(await repo.setting(f'stars_order:{pid}', '{}'))
        if p.user_id == user.id and stored.get('actor') == user.id and stored.get('rub') == p.amount and stored == terms and not stored.get('sid'):
            return
        raise ValueError('Параметры оплаченного счёта Telegram Stars не подтверждены.')
    marker=await repo.setting(f'widget_order:{pid}')
    order=json.loads(await repo.setting(f'order:{pid}','{}'))
    intent=json.loads(await repo.setting(marker,'{}')) if marker.startswith('widget_intent:') else {}
    fields=('months','tariff_id','sid','amount','total','balance_applied','devices','traffic_bytes','bot_test_access','widget_intent','antig_surcharge')
    if (not p or p.user_id!=user.id or p.status!='order_processing' or p.amount!=terms.get('total')
        or intent.get('actor')!=user.id or intent.get('payment')!=pid or terms.get('widget_intent')!=marker
        or terms.get('sid')!=0 or terms.get('balance_applied')!=0
        or any(order.get(k)!=terms.get(k) for k in fields)):
        raise ValueError('Параметры оплаченного виджета не подтверждены.')


async def render(service,action,user):
    p=action.split(':')
    if len(p) not in (4,5) or p[0]!='wpay':raise ValueError('Неверная ссылка оплаты.')
    try:tid,anti=int(p[2]),int(p[3]);accepted=int(p[4]) if len(p)==5 else None
    except ValueError:raise ValueError('Неверный выбор подписки.') from None
    return await prepare(service.repo,service.settings,user,p[1],tid,anti,accepted)
