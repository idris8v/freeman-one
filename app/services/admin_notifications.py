"""Owner operations chat: rich event cards and existing website digest snapshots."""
import json,re
from html import escape
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select,update
from sqlalchemy.exc import SQLAlchemyError
from app.database.models import Activity,ChatNotice,User,Payment,Subscription,VlessCredential,AccessKey,AppSetting,now
from app.services.screens import Screen,money,duration
from app.services.accounts import display_freeman_id
TZ=ZoneInfo('Europe/Moscow')

async def bind(repo,target):
    # New bindings start now. Never disclose a historical backlog to a new group.
    from app.services.activity import baseline_payments
    await baseline_payments(repo)
    await repo.set_setting('history_chat_id',str(target))
    await repo.set_setting('history_digest_after',now().isoformat())
    await repo.set_setting('history_events_after',now().isoformat())
    await repo.session.execute(update(Activity).where(Activity.queued==False).values(queued=True))
    await repo.session.execute(update(ChatNotice).where(ChatNotice.kind.in_(('history','history_digest')),ChatNotice.status.in_(('pending','failed'))).values(status='cancelled'))

async def queue_digest(repo):
    target=await repo.setting('history_chat_id')
    if not target or await repo.setting('history_daily_enabled','1')!='1':return
    after=await repo.setting('history_digest_after')
    if not after:
        await repo.set_setting('history_digest_after',now().isoformat());return
    for row in await repo.session.scalars(select(AppSetting).where(AppSetting.key.startswith('admin_digest:report:')).order_by(AppSetting.key.desc()).limit(31)):
        try:
            value=json.loads(row.value)
            stamp=datetime.fromisoformat(value['created_at']).astimezone(timezone.utc)
            if stamp<datetime.fromisoformat(after).astimezone(timezone.utc):continue
            day=value['id'];datetime.strptime(day,'%Y-%m-%d')
        except (ValueError,KeyError,TypeError):continue
        dedup='history_digest:'+day
        if not await repo.session.scalar(select(ChatNotice.id).where(ChatNotice.dedup==dedup)):
            await repo.add(ChatNotice(dedup=dedup,target_id=int(target),kind='history_digest',text='Ежедневный отчёт '+day))

async def customer(repo,user):
    if not user:return 'Клиент удалён'
    return ('<b>Клиент:</b> '+escape(user.display_name or 'Клиент')+
        '\n\n<b>Freeman ID:</b> <code>'+escape(display_freeman_id(user.freeman_id) or '—')+'</code>'+
        '\n\n<b>Telegram ID:</b> <code>'+escape(str(user.telegram_id) if user.telegram_id and user.telegram_id>0 else 'Не привязан')+'</code>'+
        ('\n\n<b>Username:</b> @'+escape(user.username) if user.username else ''))


def contacts(user):
    if not user:return []
    if user.username and re.fullmatch(r'[A-Za-z0-9_]{5,32}',user.username):
        return [[('💬 Написать клиенту','url:https://t.me/'+user.username)]]
    if user.telegram_id and user.telegram_id>0:
        return [[('👤 Контакт Telegram','url:tg://user?id='+str(user.telegram_id))]]
    return []


async def issue_details(repo,sub,payment=None):
    from app.database.models import AdminAudit
    snap={}
    if payment:
        try:snap=json.loads(await repo.setting(f'sale_snapshot:{payment.id}','{}'))
        except (ValueError,TypeError):pass
    c=await repo.session.scalar(select(VlessCredential).where(VlessCredential.subscription_id==sub.id))
    key=await repo.session.get(AccessKey,c.access_key_id) if c else None
    name=await repo.setting(f'subscription_name:{sub.id}','') or (key.name if key else 'Подписка')
    tariff=await repo.tariff_by_id(sub.tariff_id) if sub.tariff_id else None
    term=await repo.setting(f'subscription_duration:{sub.id}','') or snap.get('tariff_name') or (duration(tariff.months) if tariff else 'Пробный период' if sub.kind=='trial' else 'Индивидуальный срок')
    text='\n\n<b>Подписка:</b> '+escape(name)+'\n<b>Срок:</b> '+escape(term)+f'\n<b>Устройства:</b> {sub.device_limit}\n<b>До:</b> {sub.expires_at:%d.%m.%Y %H:%M} UTC'
    actor=None
    if snap.get('recorded_by'):
        actor=await repo.session.get(User,int(snap['recorded_by']))
    if not actor and (not payment or payment.status=='manual_paid'):
        conditions=[(AdminAudit.action=='admin_free') & (AdminAudit.target==str(sub.id))]
        if key:
            conditions.append((AdminAudit.action=='client_grant') & AdminAudit.details.like(f'key={key.id};%'))
        if c:
            conditions.append((AdminAudit.action=='bot_vless_grant') & (AdminAudit.target==str(c.id)))
        from sqlalchemy import or_
        audit=await repo.session.scalar(select(AdminAudit).where(or_(*conditions)).order_by(AdminAudit.id.desc()).limit(1))
        if audit:
            actor=await repo.session.scalar(select(User).where(User.telegram_id==audit.actor_telegram_id))
            if not actor:text+='\n\n<b>Выдал администратор:</b> <code>'+str(audit.actor_telegram_id)+'</code>'
    if actor:
        text+='\n\n<b>Выдал:</b> '+escape(actor.display_name)+' · <code>'+escape(display_freeman_id(actor.freeman_id) or str(actor.id))+'</code>'
        if actor.username:text+=' · @'+escape(actor.username)
    elif payment and payment.status in ('order_paid','stars_paid','balance_paid','succeeded'):text+='\n\n<b>Выдача:</b> автоматически после оплаты'
    elif sub.kind=='trial':text+='\n\n<b>Выдача:</b> активация пробного периода клиентом'
    elif '<b>Выдал администратор:</b>' not in text:text+='\n\n<b>Кто выдал:</b> не сохранено в исходной операции'
    if c and c.subscription_url:text+='\n\n<b>Ссылка подписки</b>\n<code>'+escape(c.subscription_url)+'</code>'
    return text

async def renewal_details(repo,sub,data):
    text='\n\n<b>Подписка:</b> '+escape(data['name'])+f'\n<b>ID подписки:</b> <code>{sub.id}</code>'
    text+='\n\n<b>Добавлено:</b> '+escape(data['added_term'])+'\n<b>Устройства:</b> '+str(data['devices'])
    end=datetime.fromisoformat(data['expires_at']).astimezone(TZ)
    text+='\n<b>Действует до:</b> '+end.strftime('%d.%m.%Y %H:%M')+' МСК'
    actor=await repo.session.get(User,data['recorded_by'])
    if actor:
        text+='\n\n<b>Продлил:</b> '+escape(actor.display_name)+' · <code>'+escape(display_freeman_id(actor.freeman_id) or str(actor.id))+'</code>'
        if actor.username:text+=' · @'+escape(actor.username)
    text+='\n\n<b>Учёт:</b> '+('Продажа · '+money(data['total']) if data['accounting']=='sale' else 'Бесплатно · без продажи')
    if data.get('subscription_url'):text+='\n\n<b>Ссылка подписки</b>\n<code>'+escape(data['subscription_url'])+'</code>'
    return text

def compact_event(text,rows):
    from aiogram.types import InputRichMessage,InputRichBlockDetails
    from app.bot.rich_copy import text_blocks
    title,_,details=text.partition('\n\n')
    def field(label):
        match=re.search(r'<b>'+re.escape(label)+r':</b>\s*([^\n]+)',details)
        return match.group(1) if match else ''
    term=field('Добавлено') or field('Срок')
    devices=field('Устройства')
    price=field('Сумма платежа') or ('Бесплатно' if 'Бесплатно · без продажи' in details else '')
    main=title
    if term:main+='\n'+term+(' · '+devices+' устр.' if devices else '')
    elif field('Подписка'):main+='\n'+field('Подписка')
    elif field('Клиент'):main+='\n'+field('Клиент')
    if price:main+='\n<b>'+price+'</b>'
    debt=field('Долг после выдачи')
    if debt:main+='\n💼 Долг после выдачи: <b>'+debt+'</b>'
    link=re.search(r'\n\n<b>Ссылка подписки</b>\n<code>.*?</code>',details,re.S)
    if link:
        main+=link.group(0)
        details=details[:link.start()]+details[link.end():]
    blocks=text_blocks(main)
    if details.strip():blocks.append(InputRichBlockDetails(summary='Подробности',blocks=text_blocks(details.strip()),is_open=False))
    # Legacy HTML fallback keeps the same information in an expandable quote.
    fallback=main+('\n\n<blockquote expandable>'+details.strip()+'</blockquote>' if details.strip() else '')
    return Screen(fallback,rows,rich=InputRichMessage(blocks=blocks,skip_entity_detection=True),structured=True)

async def sale_revenue_at_payment(repo, payment, snapshot):
    from sqlalchemy import text
    from datetime import datetime
    from app.web.admin_reports import SALES, aware, period_bounds

    stamp = datetime.fromisoformat(snapshot['paid_at']) if snapshot.get('paid_at') else payment.created_at
    day = aware(stamp).astimezone(TZ).date()
    lo, hi = period_bounds('custom', day.isoformat(), day.isoformat())
    params = dict(lo=lo, hi=hi, kind='all', source='all', months=-1, devices=0,
                  channel='all', sale_type='all', payment_id=str(payment.id))
    query = SALES + """
    SELECT sum(s.amount)
    FROM selected anchor
    JOIN selected s ON s.dt < anchor.dt
      OR (s.dt = anchor.dt AND (
          s.id LIKE 'B%' OR CAST(NULLIF(ltrim(s.id, 'B'), '') AS bigint) <= CAST(anchor.id AS bigint)
      ))
    WHERE anchor.id = :payment_id
    """
    async with repo.session.begin_nested():
        revenue = await repo.session.scalar(text(query), params)
    return revenue, day


def referral_identity(user):
    if not user or user.status == 'deleted':
        return 'Клиент недоступен'
    identity = display_freeman_id(user.freeman_id)
    return ' · '.join(escape(str(value)) for value in (user.display_name, identity) if value)


def referral_actions(settings, invited, inviter):
    if not settings or not settings.bot_username:
        return []
    prefix = f'url:https://t.me/{settings.bot_username}?start=ops_client_'
    return [[(label, prefix + str(user.id))] for label, user in (
        ('👤 Приглашённый клиент', invited), ('🤝 Кто пригласил', inviter)
    ) if user and user.status != 'deleted']


def rich_link_card(body, rows):
    from aiogram.types import InputRichBlockButtons, RichMessageButton
    from app.services.admin_client_grant import rich

    screen = rich(body, [])
    for row in rows:
        label, target = row[0]
        url = target.removeprefix('url:')
        screen.rich.blocks.append(InputRichBlockButtons(buttons=[
            RichMessageButton(text=label, url=url, style='primary')
        ]))
        screen.text += f'\n<a href="{escape(url, quote=True)}">{escape(label)}</a>'
    return screen


def referral_card(body, settings, invited, inviter):
    return rich_link_card(body, referral_actions(settings, invited, inviter))


async def withdrawal_screen(repo, event, settings):
    from app.database.models import AdminAudit, BalanceTransaction, Withdrawal

    category, object_id = event.dedup.split(':')
    operator = None
    if category == 'admin.action':
        audit = await repo.session.get(AdminAudit, int(object_id))
        if not audit or audit.action not in ('withdrawal_paid', 'withdrawal_rejected') or not audit.target.isdecimal():
            return None
        withdrawal_id = int(audit.target)
        kind = audit.action
        operator = await repo.user(audit.actor_telegram_id)
    else:
        transaction = await repo.session.get(BalanceTransaction, int(object_id))
        if not transaction or transaction.type != 'withdrawal_hold':
            return None
        match = re.fullmatch(r'Резерв на вывод #(\d+)', transaction.description)
        if not match:
            return None
        withdrawal_id = int(match[1])
        kind = 'requested'
    item = await repo.session.get(Withdrawal, withdrawal_id)
    if not item or (category == 'balance.event' and transaction.user_id != item.user_id):
        return None
    customer_user = await repo.session.get(User, item.user_id)
    title = {'requested': '💸 Новая заявка на выплату', 'withdrawal_paid': '✅ Выплата подтверждена',
             'withdrawal_rejected': '↩️ Заявка на выплату отклонена'}[kind]
    stamp = event.created_at
    stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
    body = (f'<b>{title}</b>\n\n<b>Заявка:</b> №{item.id}'
            f'\n<b>Клиент:</b> {referral_identity(customer_user)}\n<b>Сумма:</b> {money(item.amount)}'
            f'\n<b>Время:</b> {stamp.astimezone(TZ):%d.%m.%Y %H:%M} МСК')
    if operator:
        body += f'\n<b>Провёл:</b> {referral_identity(operator)}'
    if kind == 'withdrawal_rejected':
        body += '\n\nРезерв возвращён на баланс клиента.'
    rows = []
    if settings and settings.bot_username:
        base = f'url:https://t.me/{settings.bot_username}?start=ops_'
        rows.append([('Открыть заявку',base+f'payout_{item.id}')])
        if customer_user and customer_user.status != 'deleted':
            rows.append([('Карточка клиента',base+f'client_{customer_user.id}')])
    return rich_link_card(body, rows)


async def referral_purchase_label(repo, reward):
    from app.services.bot_catalog import catalog_term, devices_label

    payment = await repo.session.get(Payment, reward.payment_id)
    if not payment:
        return 'Тип покупки не сохранился'
    snapshot = json.loads(await repo.setting(f'sale_snapshot:{payment.id}', '{}'))
    if snapshot.get('kind') == 'proxy' or payment.status == 'proxy_order_paid':
        return 'Telegram Proxy'
    order_key = 'stars_order' if payment.status == 'stars_paid' else 'order'
    order = json.loads(await repo.setting(f'{order_key}:{payment.id}', '{}'))
    snapshot_profile = snapshot.get('bot_test_access') or order.get('bot_test_access') or {}
    if snapshot.get('sale_category') == 'antig_addon' or (snapshot.get('antig_addon') and not snapshot.get('months')):
        return 'Антиглушилка к действующей подписке'
    months = snapshot.get('months') or order.get('months')
    devices = snapshot.get('devices') or order.get('devices') or order.get('device_limit')
    label = ['VPN']
    if type(devices) is int and devices > 0:
        label.append(devices_label(devices))
    if type(months) is int and months > 0:
        label.append(catalog_term(months))
    elif str(snapshot.get('days', '')).isdigit():
        label.append(str(snapshot['days']) + ' дн.')
    if len(snapshot_profile.get('squads', [])) == 2 or snapshot.get('antig_addon'):
        label.append('с антиглушилкой')
    return ' · '.join(label)


async def referral_total(repo, inviter_id, invited_id):
    from sqlalchemy import func
    from app.database.models import ReferralReward

    amount = await repo.session.scalar(select(func.sum(ReferralReward.amount)).where(
        ReferralReward.inviter_id == inviter_id, ReferralReward.referred_user_id == invited_id,
        ReferralReward.status == 'credited'))
    return int(amount or 0)


async def referral_transaction_screen(repo, transaction_id, settings):
    from app.database.models import BalanceTransaction, ReferralReward
    transaction = await repo.session.get(BalanceTransaction, transaction_id)
    if not transaction or transaction.type not in ('referral_reward', 'referral_reversal'):
        return None
    query = select(ReferralReward).where(ReferralReward.transaction_id == transaction.id)
    if transaction.type == 'referral_reversal':
        payment = re.search(r'платежа #(d+)$', transaction.description)
        if not payment:
            return None
        query = select(ReferralReward).where(ReferralReward.payment_id == int(payment[1]),
                                             ReferralReward.inviter_id == transaction.user_id)
    reward = await repo.session.scalar(query)
    if not reward:
        return None
    inviter = await repo.session.get(User, reward.inviter_id)
    invited = await repo.session.get(User, reward.referred_user_id) if reward.referred_user_id else None
    undone = transaction.type == 'referral_reversal'
    title = '↩️ Реферальный бонус отменён' if undone else '💸 Начислен реферальный бонус'
    total = await referral_total(repo, reward.inviter_id, reward.referred_user_id)
    purchase = {'first': 'Первая покупка', 'repeat': 'Повторная покупка'}.get(reward.purchase_type, 'Покупка')
    plan = await referral_purchase_label(repo, reward)
    balance = money(await repo.balance(reward.inviter_id))
    body = (f'<b>{title}</b>\n\n<b>Кто пригласил:</b> {referral_identity(inviter)}'
            f'\n<b>Кого пригласили:</b> {referral_identity(invited)}\n\n'
            f'<b>{purchase}:</b> {escape(plan)} · {money(reward.payment_amount)}'
            f'\n<b>Бонус за покупку:</b> {money(transaction.amount)} · {reward.percentage}%'
            f'\n<b>Всего за клиента:</b> {money(total)}'
            f'\n<b>Баланс пригласившего:</b> {balance}')
    return referral_card(body, settings, invited, inviter)


async def event_screen(repo,event,settings=None):
    p=event.dedup.split(':');text='';user=None
    if p[0] in ('balance.event', 'admin.action'):
        withdrawal = await withdrawal_screen(repo, event, settings)
        if withdrawal:
            return withdrawal
    if p[0]=='balance.event':
        referral = await referral_transaction_screen(repo, int(p[1]), settings)
        if referral:
            return referral
    if p[0]=='sale.corrected':
        from app.services.admin_client_grant import rich
        data=json.loads(event.text)
        total=data.get('revenue')
        if total is None:
            from app.services.bot_admin_checkout import today_summary
            try:total,_=await today_summary(repo)
            except SQLAlchemyError:pass
        text=(f'✏️ <b>Цена продажи №{int(p[1])} изменена</b>\n\n'
              f'{money(data["old"])} → <b>{money(data["new"])}</b>')
        if total is not None:text+=f'\n\n📊 Выручка за сегодня: <b>{money(total)}</b>'
        return rich(text,[])
    if p[0]=='infra.alert':
        from app.services.admin_client_grant import rich
        title,_,details=event.text.partition('\n')
        return rich(f'<b>{escape(title)}</b>\n\n{escape(details)}',[])
    if p[0]=='user.registered':
        user=await repo.session.get(User,int(p[1]))
        if user and user.referrer_id and user.referrer_id != user.id:
            inviter = await repo.session.get(User, user.referrer_id)
            total = await referral_total(repo, user.referrer_id, user.id)
            balance = money(await repo.balance(user.referrer_id))
            body = ('🤝 <b>Клиент по приглашению</b>\n\n'
                    f'<b>Кто пригласил:</b> {referral_identity(inviter)}\n'
                    f'<b>Приглашённый клиент:</b> {referral_identity(user)}\n\n'
                    f'<b>Начислено за клиента:</b> {money(total)}'
                    f'\n<b>Баланс пригласившего:</b> {balance}')
            return referral_card(body, settings, user, inviter)
        text='👤 <b>Новый клиент</b>\n\n'+await customer(repo,user)
    elif p[0]=='subscription.created':
        sub=await repo.session.get(Subscription,int(p[1]))
        if sub:
            user=await repo.session.get(User,sub.user_id)
            partner=json.loads(await repo.setting(f'partner_issue_notice:{sub.id}','{}'))
            title='🤝 <b>Партнёр выдал подписку</b>' if partner else '📦 <b>Выдана подписка</b>'
            text=title+'\n\n'+await customer(repo,user)
            text+='\n\n<b>ID подписки:</b> <code>'+str(sub.id)+'</code>'
            text+='\n<b>Тип выдачи:</b> '+('Пробная' if sub.kind=='trial' else 'Выдача администратором' if sub.kind=='admin' else 'Подписка')
            if partner:
                text+='\n<b>Сумма платежа:</b> Начислено в долг '+money(partner['charged'])
                text+='\n<b>Долг после выдачи:</b> '+money(partner['debt'])
            elif sub.kind in ('admin','trial'):text+='\n<b>Сумма платежа:</b> Бесплатно'
            text+=await issue_details(repo,sub)
    elif p[0]=='subscription.renewed':
        data=json.loads(event.text)
        sub=await repo.session.get(Subscription,data['subscription_id'])
        if sub:
            user=await repo.session.get(User,data['user'])
            text='🔄 <b>Подписка продлена бесплатно</b>\n\n'+await customer(repo,user)+await renewal_details(repo,sub,data)
    elif p[0]=='payment':
        payment=await repo.session.get(Payment,int(p[1]))
        if payment:
            user=await repo.session.get(User,payment.user_id)
            labels={'refunded':'↩️ Возврат платежа','failed':'❌ Неуспешный платёж','manual_paid':'✅ Ручная продажа'}
            title=labels.get(payment.status,'✅ Покупка подписки' if payment.subscription_id else '💰 Пополнение баланса')
            snapshot=json.loads(await repo.setting(f'sale_snapshot:{payment.id}','{}'))
            gift=snapshot.get('gift')
            if gift:title='🎁 Куплена VPN-подписка в подарок'
            elif snapshot.get('renewal') and payment.status not in ('refunded','failed'):title='🔄 Подписка продлена'
            text='<b>'+title+'</b>\n\n'+await customer(repo,user)
            text+='\n\n<b>ID платежа:</b> <code>'+str(payment.id)+'</code>\n\n<b>Сумма платежа:</b> '+money(payment.amount)
            if (payment.status in ('order_paid','stars_paid','balance_paid','manual_paid','proxy_order_paid')
                    and p[-1] == payment.status and payment.amount > 0
                    and not snapshot.get('test') and not snapshot.get('deleted')):
                try:
                    revenue, day = await sale_revenue_at_payment(repo, payment, snapshot)
                except (SQLAlchemyError, ValueError):
                    pass
                else:
                    if revenue is not None:
                        period = 'сегодня' if day == now().astimezone(TZ).date() else day.strftime('%d.%m.%Y')
                        text += f' (за {period} {money(revenue)})'
            if gift:
                if snapshot.get('months'):text+='\n<b>Срок:</b> '+escape(duration(snapshot['months']))
                if snapshot.get('devices'):text+='\n<b>Устройства:</b> '+str(snapshot['devices'])
            if payment.subscription_id:
                sub=await repo.session.get(Subscription,payment.subscription_id)
                if sub:text+=await renewal_details(repo,sub,snapshot) if snapshot.get('added_term') else await issue_details(repo,sub,payment)
    if not text:text=escape(event.text)
    stamp=event.created_at.replace(tzinfo=timezone.utc) if not event.created_at.tzinfo else event.created_at
    text+='\n\n<i>'+stamp.astimezone(TZ).strftime('%d.%m.%Y %H:%M')+' МСК</i>'
    rows = contacts(user)
    if p[0] == 'payment' and user and settings and settings.bot_username:
        rows.insert(0, [('👤 Карточка клиента', f'url:https://t.me/{settings.bot_username}?start=ops_client_{user.id}')])
    return compact_event(text, rows)

async def digest_screen(repo,day):
    raw=await repo.setting('admin_digest:report:'+day)
    if not raw:raise ValueError('Отчёт ещё не подготовлен сайтом.')
    value=json.loads(raw);d=value['summary']
    text='📊 <b>Итоги '+escape(datetime.strptime(day,'%Y-%m-%d').strftime('%d.%m.%Y'))+'</b>\n\nЗа прошедший день · московское время.\n\n'
    rows=[('Новые клиенты',str(d.get('new_customers',0))),('Продажи',str(d.get('sales',0))),('Выручка',money(d.get('revenue',0))),('Покупатели',str(d.get('buyers',0))),('Продления',str(d.get('renewals',0))),('Средний чек',money(d.get('average',0)))]
    from sqlalchemy import func
    from app.web.admin_reports import period_bounds
    lo, hi = period_bounds('custom', day, day)
    refunds = await repo.session.scalar(select(func.count(Activity.id)).where(
        Activity.created_at >= lo, Activity.created_at < hi, Activity.dedup.like('payment:%:refunded')))
    rows.append(('Возвраты, зафиксированные ботом', str(refunds or 0)))
    text+='\n\n'.join('<b>'+a+':</b> '+b for a,b in rows)
    if d.get('popular'):text+='\n\n🏆 <b>Популярный тариф:</b> '+escape(d['popular'])
    from app.bot.rich_copy import text_blocks
    from app.services.bot_catalog import rich_table
    from aiogram.types import InputRichMessage
    rich=rich_table('📊 Итоги '+datetime.strptime(day,'%Y-%m-%d').strftime('%d.%m.%Y'),'Прошедший день · МСК',['Показатель','Итого'],[list(r) for r in rows],note='🏆 Популярный тариф: '+(d.get('popular') or 'Нет продаж'))
    return Screen(text,[],rich=rich,structured=True)

async def render(repo,settings,actor,action):
    if not settings.is_owner(actor.telegram_id):raise PermissionError('Только администратор управляет уведомлениями.')
    p=action.split(':');op=p[2] if len(p)>2 else ''
    allowed={'clients','sales','subscriptions','referrals','withdrawals','daily'}
    if op in allowed:
        key='history_daily_enabled' if op=='daily' else 'history_category:'+op
        await repo.set_setting(key,'0' if await repo.setting(key,'1')=='1' else '1')
    if op=='disconnect':return Screen('Отключить чат уведомлений? Новые события сохранятся в базе, но сообщения в группу отправляться не будут.',[[('Отключить','admin:notifications:disconnect_confirm')],[('← Назад','admin:notifications')]],structured=True)
    if op=='disconnect_confirm':
        await repo.set_setting('history_chat_id','')
        await repo.session.execute(update(ChatNotice).where(ChatNotice.kind.in_(('history','history_digest')),ChatNotice.status.in_(('pending','failed'))).values(status='cancelled'))
    target=await repo.setting('history_chat_id');rows=[]
    for field,label in [('clients','👤 Регистрации'),('sales','💳 Платежи'),('subscriptions','🔑 Выдачи ключей'),('referrals','🤝 Реферальные бонусы'),('withdrawals','💸 Заявки и выплаты'),('daily','📊 Ежедневный отчёт')]:
        key='history_daily_enabled' if field=='daily' else 'history_category:'+field
        rows.append([(label+' · '+('Вкл.' if await repo.setting(key,'1')=='1' else 'Выкл.'),'admin:notifications:'+field)])
    if target:rows.append([('Отключить чат','admin:notifications:disconnect')])
    rows.append([('← Назад','admin:more')])
    return Screen('🔔 <b>Уведомления</b>\n\n'+('Чат подключён.' if target else 'Чат пока не подключён.')+'\n\nСоздайте закрытую группу, добавьте бота и отправьте в ней <code>/bind_history</code> от администратора.\n\nРегистрации и подтверждённые покупки приходят автоматически. Ежедневный отчёт берётся из готовой сводки сайта за прошедший день по МСК. Старые события при подключении не рассылаются.',rows,structured=True)
