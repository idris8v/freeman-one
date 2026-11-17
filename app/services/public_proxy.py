"""Public Telegram Proxy connection and reusable invitation for the bot."""
from urllib.parse import parse_qs, urlencode, urlsplit

from app.services.admin_client_grant import rich


SETTING = 'bot_public_proxy_url'
START = 'free_proxy'
INVITE_TEXT = ('✈️ Telegram Proxy от Freeman ONE\n\n'
               'Подключите бесплатный прокси, чтобы пользоваться Telegram без VPN.')


def valid_url(value):
    try:
        url = urlsplit(value)
        query = parse_qs(url.query)
        return (len(value) <= 1000 and not any(c.isspace() for c in value)
                and url.scheme == 'https' and url.netloc == 't.me' and url.path in ('/proxy', '/webproxy')
                and not url.fragment and bool(query.get('server')) and bool(query.get('secret'))
                and (url.path == '/webproxy' or bool(query.get('port'))))
    except ValueError:
        return False


async def connection_url(repo):
    value = await repo.setting(SETTING, '')
    return value if valid_url(value) else ''


def connect_screen(url):
    target = 'url:' + url
    screen = rich('✈️ <b>Бесплатный Telegram Proxy</b>\n\n'
                  'Нажмите «Подключить прокси» и подтвердите подключение в Telegram.',
                  [[('✈️ Подключить прокси', target)], [('← Главное меню', 'home')]])
    screen.button_options[target] = {'style': 'primary'}
    return screen


async def queue_purchase_notice(repo, user, payment, subscription):
    import json
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from app.database.models import ChatNotice, now
    from app.services.reminder_policy import utc

    if not subscription or subscription.kind != 'paid' or not user.telegram_id or user.telegram_id <= 0:
        return
    if payment.status not in ('order_paid', 'balance_paid', 'stars_paid') or not await connection_url(repo):
        return
    if await repo.setting(f'notifications:{user.id}', '1') != '1':
        return
    snapshot = json.loads(await repo.setting(f'sale_snapshot:{payment.id}', '{}'))
    if snapshot.get('renewal') or snapshot.get('test') or snapshot.get('deleted') or not snapshot.get('paid_at'):
        return
    try:
        stamp = utc(datetime.fromisoformat(snapshot['paid_at']))
    except (TypeError, ValueError):
        return
    if now() - stamp > timedelta(minutes=15):
        return
    dedup = f'proxy.gift:{user.id}'
    if not await repo.session.scalar(select(ChatNotice.id).where(ChatNotice.dedup == dedup)):
        await repo.add(ChatNotice(dedup=dedup, target_id=user.telegram_id, kind='public_proxy_offer',
                                  text='Бесплатный Telegram Proxy', available_at=stamp + timedelta(minutes=15)))


async def purchase_notice_screen(repo, job):
    from app.database.models import User
    from app.services.proxies import default_proxy_photo
    from app.services.screens import Screen

    user = await repo.session.get(User, int(job.dedup.rsplit(':', 1)[1]))
    url = await connection_url(repo)
    if not url or not user or user.status != 'active' or user.telegram_id != job.target_id:
        return None
    if await repo.setting(f'notifications:{user.id}', '1') != '1':
        return None
    photo = None
    if await repo.setting('media_type:proxy', 'photo') == 'photo':
        photo = await repo.setting('media_file:proxy') or await repo.setting('image:proxy')
    target = 'url:' + url
    return Screen('🎁 <b>Бесплатный Telegram Proxy</b>\n\n'
                  'Пользуйтесь Telegram даже с выключенным VPN. '
                  'Нажмите кнопку и подтвердите подключение в Telegram.',
                  [[('✈️ Подключить прокси', target)]], photo=photo or default_proxy_photo(),
                  structured=True, button_options={target: {'style': 'primary'}})


def admin_screen(settings, url):
    invitation = f'https://t.me/{settings.bot_username}?start={START}'
    share = 'url:https://t.me/share/url?' + urlencode({'url': invitation, 'text': INVITE_TEXT})
    message = INVITE_TEXT + '\n\n' + invitation
    actions = [[('📣 Поделиться приглашением', share)],
               [('📋 Скопировать готовый текст', 'copy:' + message)]]
    if url:
        actions.append([('✈️ Проверить подключение', 'url:' + url)])
    actions.extend([[('✏️ Изменить ссылку подключения', 'admin:input:public_proxy')],
                    [('← Администратор', 'admin:home')]])
    return rich('📣 <b>Бесплатный Telegram Proxy</b>\n\n'
                'Поделитесь приглашением в рассылке или в чате. Каждый, кто откроет бота по ссылке, '
                'получит кнопку подключения.\n\n'
                f'<code>{invitation}</code>',
                actions)


def prompt():
    return rich('✏️ <b>Ссылка подключения к Telegram Proxy</b>\n\n'
                'Пришлите новую ссылку Telegram Proxy одним сообщением. '
                'Она будет использоваться в кнопке бота и во всех ранее созданных приглашениях.',
                [[('← Telegram Proxy', 'admin:p:home')]])


async def replace(repo, settings, actor, value):
    from app.database.models import AdminAudit
    url = value.strip()
    if url.startswith(('tg://proxy?', 'tg://webproxy?')):
        url = 'https://t.me/' + url.removeprefix('tg://')
    if not valid_url(url):
        raise ValueError('Пришлите ссылку t.me/proxy или t.me/webproxy с параметрами подключения.')
    await repo.set_setting(SETTING, url)
    await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id, action='public_proxy_link',
                              target=SETTING, details='updated'))
    screen = admin_screen(settings, url)
    return rich('✅ <b>Ссылка обновлена</b>\n\n' + screen.text, screen.rows)
