"""One first-start invitation to a 72-hour VPN trial with Antiglushilka."""
import json
from datetime import timedelta

from sqlalchemy import String, cast, literal, select

from app.database.models import ChatNotice, FunnelEvent, Payment, Subscription, User, VlessCredential, now
from app.services.admin_client_grant import rich

HOURS = 72
DEVICES = 3
ACTION = 'trial_offer:claim'
CONVERSION_KIND = 'trial_convert'
CONVERSION_LABEL = '✨ Оставить подключение'
PURCHASED = ('order_paid', 'balance_paid', 'stars_paid', 'manual_paid',
             'proxy_order_paid', 'succeeded', 'refunded')


def notice_key(user_id):
    return f'trial.offer:{user_id}'


async def trial_hours(repo):
    return int(await repo.setting('bot_trial_hours', str(HOURS)))


def trial_duration(hours):
    return '3 дня' if hours == HOURS else f'{hours} ч.'


async def eligible(repo, settings, user, *, reminder=False):
    if not user or user.status!='active' or user.trial_used or not user.telegram_id or user.telegram_id<=0:
        return False
    if settings.role(user.telegram_id) or await repo.setting(f'staff_role:{user.telegram_id}'):
        return False
    if (await repo.setting('trial_enabled','1')!='1'
            or await repo.setting(f'user_archived:{user.id}')=='1'
            or (reminder and await repo.setting(f'notifications:{user.id}','1')!='1')):
        return False
    if await repo.session.scalar(select(Subscription.id).where(Subscription.user_id==user.id).limit(1)):
        return False
    return not await repo.session.scalar(select(Payment.id).where(
        Payment.user_id==user.id,Payment.status.in_(PURCHASED)).limit(1))


async def queue(repo,settings,user):
    if not await eligible(repo,settings,user,reminder=True):
        return
    user=await repo.session.get(User,user.id,with_for_update=True,populate_existing=True)
    if await repo.session.scalar(select(FunnelEvent.id).where(
            FunnelEvent.user_id==user.id,FunnelEvent.stage=='start').limit(1)):
        return
    dedup=notice_key(user.id)
    if await repo.session.scalar(select(ChatNotice.id).where(ChatNotice.dedup==dedup)):
        return
    if await eligible(repo,settings,user,reminder=True):
        await repo.add(ChatNotice(dedup=dedup,target_id=user.telegram_id,kind='trial_offer',
                                  text='Пробная подписка',available_at=now()+timedelta(minutes=30)))


async def notice_screen(repo,settings,job):
    user=await repo.session.get(User,int(job.dedup.rsplit(':',1)[1]))
    if not user or user.telegram_id!=job.target_id or not await eligible(repo,settings,user,reminder=True):
        return None
    duration=trial_duration(await trial_hours(repo))
    screen=rich(f'🎁 <b>{duration} VPN в подарок</b>\n\n'
                'Попробуйте VPN с антиглушилкой на <b>трёх устройствах</b>.\n\n'
                'Пробный период начнётся после активации. Подписка бесплатная.',
                [[('🎁 Получить пробную подписку',ACTION)]])
    screen.button_options={ACTION:{'style':'success'}}
    return screen


async def convertible(repo, settings, sub, user):
    from app.services.billing import BillingService

    if not user or sub.kind != 'trial' or user.status != 'active' or not user.telegram_id or user.telegram_id <= 0:
        return False
    if (settings.role(user.telegram_id) or await repo.setting(f'staff_role:{user.telegram_id}')
            or await repo.setting(f'user_archived:{user.id}') == '1'):
        return False
    try:
        _, key = await BillingService(repo, settings).valid(user, sub.id)
    except ValueError:
        return False
    if key.mode != 'vless':
        return False
    if await repo.session.scalar(select(Payment.id).where(
        Payment.subscription_id == sub.id, Payment.user_id == user.id, Payment.status.in_(PURCHASED),
    ).limit(1)):
        return False
    return bool(await repo.session.scalar(select(VlessCredential.id).where(
        VlessCredential.subscription_id == sub.id, VlessCredential.access_key_id == key.id,
        VlessCredential.user_id == user.id, VlessCredential.status == 'active',
    )))


async def conversion_screen(repo, settings, job):
    from app.services.vless_creation import utc

    sub = await repo.session.get(Subscription, int(job.dedup.split(':')[1]))
    user = await repo.session.get(User, sub.user_id) if sub else None
    if not user or user.telegram_id != job.target_id or not await convertible(repo, settings, sub, user):
        return None
    if (await repo.setting('reminders_enabled', '1') != '1'
            or await repo.setting(f'notifications:{user.id}', '1') != '1'
            or not now() < utc(sub.expires_at) <= now() + timedelta(hours=5)):
        return None
    target = f'renew:{sub.id}'
    screen = rich('⏳ <b>Пробная подписка скоро закончится</b>\n\n'
                  'Выберите тариф, чтобы продолжить пользоваться VPN. '
                  'Текущий ключ и ссылка на подключение сохранятся.',
                  [[(CONVERSION_LABEL, target)]])
    screen.button_options[target] = {'style': 'success'}
    return screen


async def prepare_conversion(repo, settings):
    if await repo.setting('reminders_enabled', '1') != '1':
        return
    stamp = now()
    notified = select(ChatNotice.id).where(
        ChatNotice.dedup == literal('trial.convert:') + cast(Subscription.id, String),
    ).exists()
    rows = await repo.session.execute(select(Subscription, User).join(User, User.id == Subscription.user_id).where(
        Subscription.kind == 'trial', Subscription.expires_at > stamp,
        Subscription.expires_at <= stamp + timedelta(hours=5), User.status == 'active',
        User.telegram_id > 0, ~notified,
        select(VlessCredential.id).where(VlessCredential.subscription_id == Subscription.id).exists(),
    ))
    for sub, user in rows:
        if await repo.setting(f'notifications:{user.id}', '1') != '1' or not await convertible(repo, settings, sub, user):
            continue
        await repo.add(ChatNotice(dedup=f'trial.convert:{sub.id}', target_id=user.telegram_id,
                                  kind=CONVERSION_KIND, text='Пробная подписка скоро закончится'))


def unavailable():
    return rich('🎁 <b>Пробное предложение недоступно</b>\n\n'
                'Посмотрите текущие подписки или выберите тариф.',
                [[('🔑 Мои ключи','subscriptions')],[('Тарифы','buy')]])


async def claim(service,actor,*,invited=True):
    from app.services.bot_antig_test import resolve
    from app.services.remnawave import RemnawaveApiError
    from app.services.vless_provision import connection, ensure

    repo,settings=service.repo,service.settings
    user=await repo.session.get(User,actor.id,with_for_update=True,populate_existing=True)
    result_key=f'trial_offer_subscription:{actor.id}'
    existing=await repo.setting(result_key)
    if existing and user and user.status=='active':
        sub=await repo.subscription(user.id,int(existing))
        return (await connection(repo,settings,sub,user) if sub else None) or unavailable()
    if not await eligible(repo,settings,user,reminder=invited):
        return unavailable()
    if invited:
        job=await repo.session.scalar(select(ChatNotice).where(ChatNotice.dedup==notice_key(actor.id)))
        if not job or job.target_id!=user.telegram_id or job.status not in ('sending','sent'):
            return unavailable()
    try:
        async with repo.session.begin_nested():
            profile=await resolve(repo,settings,True)
            sub=await service.subscriptions.activate_trial(user.id,hours=await trial_hours(repo),device_limit=DEVICES)
            if not sub:
                return unavailable()
            await repo.set_setting(f'bot_test_access:{sub.id}',json.dumps(profile))
            credential=await ensure(repo,settings,sub,user,operation_key=f'trial:{user.id}')
            if not credential:
                raise ValueError('Подключение не выдано.')
            await repo.track_funnel(user.id,'trial')
            await repo.set_setting(result_key,str(sub.id))
            screen=await connection(repo,settings,sub,user)
            if not screen:
                raise ValueError('Карточка подписки недоступна.')
            screen.text='✅ <b>Пробная подписка с антиглушилкой активирована</b>\n\n'+screen.text
            return screen
    except (ValueError,RemnawaveApiError):
        return rich('Не удалось завершить выдачу пробной подписки. Попробуйте ещё раз.',
                    [[('🔄 Повторить',ACTION if invited else 'activate')],[('💬 Поддержка','help')]])
