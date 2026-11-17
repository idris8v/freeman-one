"""Reusable Telegram Proxy invitations with one personal credential per account."""
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.database.models import AppSetting, ProxyCredential, ProxyServer, User, now
from app.database.repositories import SQLRepository
from app.services.admin_client_grant import rich
from app.services.bot_grant_config import parse_combined, parse_period
from app.services.proxies import ProxyPlacementError, ProxyService
from app.services.vless_creation import durable_session, utc


CAMPAIGN = 'proxy_campaign:'
RESERVATION = 'proxy_campaign_reservation:'
CLAIM = 'proxy_campaign_claim:'
ADMIN = 'admin:proxycampaign'
INPUT = 'admin:input:proxycampaign'
DEFAULT_WINDOW = 7
PAGE = 5
MOSCOW = ZoneInfo('Europe/Moscow')


class CampaignRetry(ValueError):
    """An activation can be resumed using the same public link."""


def campaign_key(token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{24}', token):
        raise ValueError('Рекламная ссылка недействительна.')
    return CAMPAIGN + token


def status(data):
    if data['state'] == 'closed':
        return 'Остановлена'
    if data.get('until') and datetime.fromisoformat(data['until']) <= now():
        return 'Срок истёк'
    return 'Активна' if data['state'] == 'open' else 'Черновик'


def terms(data):
    return f'{data["days"]} дн. · до {data["ip"]} уникальных IP на аккаунт'


def placement(server, ip):
    return dict(server=server.id, tcp=max(ip, server.tcp_connection_limit), attempted=True)


class ProxyCampaigns:
    def __init__(self, repo, settings):
        self.repo, self.settings, self.session = repo, settings, repo.session

    def require(self, actor):
        if not self.settings.is_owner(actor.telegram_id) or actor.status != 'active':
            raise PermissionError('Рекламные ссылки доступны администратору.')

    def link(self, token):
        campaign_key(token)
        return f'https://t.me/{self.settings.bot_username}?start=pc_{token}'

    def prompt(self, actor):
        self.require(actor)
        screen = rich(
            '📣 <b>Новая рекламная ссылка</b>\n\n'
            'Укажите лимит IP на человека, срок прокси и срок самой ссылки.\n\n'
            'Пример <code>2, 30 дней, 7 дней</code>\n\n'
            'Если третий параметр пропустить, ссылка будет действовать 7 дней. '
            'Напишите <code>бессрочно</code> третьим параметром, чтобы останавливать её вручную.',
            [[('← Рекламные ссылки', ADMIN)]],
        )
        screen.admin_input_action = INPUT
        return screen

    async def input(self, value, actor):
        self.require(actor)
        parts = value.split(',', 2)
        data = parse_combined(','.join(parts[:2]), proxy=True)
        window = DEFAULT_WINDOW
        if len(parts) == 3:
            window_value = parts[2].strip().casefold()
            if window_value in ('бессрочно', 'без срока'):
                window = None
            else:
                unit, window = parse_period(window_value)
                if unit != 'days':
                    raise ValueError('Срок рекламной ссылки укажите в днях или напишите «бессрочно».')
        token = secrets.token_urlsafe(18)
        draft = dict(actor=actor.id, state='draft', days=data['amount'], ip=data['devices'],
                     window=window, created=now().isoformat())
        await self.repo.add(AppSetting(key=campaign_key(token), value=json.dumps(draft)))
        target = f'{ADMIN}:make:{token}'
        life = f'{window} дн.' if window else 'до остановки'
        screen = rich(
            '📣 <b>Проверьте параметры</b>\n\n'
            + terms(draft) + f'\nРекламная ссылка действует {life}.\n\n'
            'Каждый аккаунт получит свой прокси один раз по этой ссылке. '
            'Срок прокси отсчитывается с активации.',
            [[('✅ Создать ссылку', target)], [('✏️ Изменить', INPUT)], [('← Рекламные ссылки', ADMIN)]],
        )
        screen.button_options[target] = {'style': 'success'}
        return screen

    async def render(self, action, actor):
        self.require(actor)
        p = action.split(':')
        if len(p) == 2 or p[2] == 'list':
            offset = max(0, int(p[3])) if len(p) > 3 else 0
            records = []
            for key, value in (await self.session.execute(select(AppSetting.key, AppSetting.value).where(
                AppSetting.key.startswith(CAMPAIGN, autoescape=True)
            ))).all():
                data = json.loads(value)
                if data['state'] != 'draft':
                    records.append((key.removeprefix(CAMPAIGN), data))
            records.sort(key=lambda item: item[1]['created'], reverse=True)
            offset = min(offset, max(0, (len(records)-1)//PAGE*PAGE))
            rows = [[(f'{status(d)} · {d["days"]} дн. · {d["ip"]} IP', f'{ADMIN}:view:{token}')]
                    for token, d in records[offset:offset+PAGE]]
            if offset:
                rows.append([('← Более новые', f'{ADMIN}:list:{max(0, offset-PAGE)}')])
            if offset + PAGE < len(records):
                rows.append([('Более ранние →', f'{ADMIN}:list:{offset+PAGE}')])
            rows += [[('➕ Создать рекламную ссылку', INPUT)], [('← Telegram Proxy', 'p:home')]]
            return rich('📣 <b>Рекламные ссылки Telegram Proxy</b>\n\n'
                        'Одна ссылка для публикации, отдельный прокси каждому получателю.', rows)

        op, token = p[2:4]
        row = await self.session.get(AppSetting, campaign_key(token), populate_existing=True,
                                     with_for_update=op in ('make', 'closeok'))
        if not row:
            raise ValueError('Рекламная ссылка не найдена.')
        data = json.loads(row.value)
        if op == 'make' and data['state'] == 'draft':
            if data['actor'] != actor.id:
                raise PermissionError('Черновик создан другим администратором.')
            stamp = now()
            if datetime.fromisoformat(data['created']) + timedelta(hours=1) <= stamp:
                raise ValueError('Черновик устарел. Создайте ссылку заново.')
            if not await ProxyService(self.repo, self.settings).matching_servers('paid'):
                raise ValueError('Сейчас нет доступного прокси-сервера для выдачи. Проверьте серверы и повторите.')
            data.update(state='open', until=(stamp+timedelta(days=data['window'])).isoformat() if data['window'] else None)
            row.value = json.dumps(data)
        if data['state'] == 'draft':
            raise ValueError('Сначала подтвердите создание ссылки.')
        if op == 'close':
            target = f'{ADMIN}:closeok:{token}'
            screen = rich('Остановить новые активации? Уже начатые выдачи и действующие прокси сохранятся.',
                          [[('Остановить ссылку', target)], [('← Назад', f'{ADMIN}:view:{token}')]])
            screen.button_options[target] = {'style': 'danger'}
            return screen
        if op == 'closeok' and data['state'] == 'open':
            data['state'] = 'closed'
            row.value = json.dumps(data)
        if op not in ('make', 'view', 'closeok'):
            raise ValueError('Действие недоступно.')
        return await self.card(token, data)

    async def card(self, token, data):
        count = await self.session.scalar(select(func.count()).select_from(AppSetting).where(
            AppSetting.key.startswith(f'{CLAIM}{token}:', autoescape=True)))
        life = datetime.fromisoformat(data['until']).astimezone(MOSCOW).strftime('До %d.%m.%Y %H:%M МСК') if data.get('until') else 'До остановки'
        state = status(data)
        body = (f'📣 <b>Рекламная ссылка Telegram Proxy</b>\n\n{terms(data)}\n{life}\n'
                f'Статус: {state.lower()}\nПолучили прокси: <b>{count}</b>')
        rows = []
        if state == 'Активна':
            link = self.link(token)
            message = ('🎁 Telegram Proxy от Freeman ONE\n\n'
                       f'Получите персональный прокси для Telegram на {data["days"]} дн. '
                       'Откройте бота по ссылке и нажмите «Старт».')
            share = 'url:https://t.me/share/url?' + urlencode(dict(url=link, text=message))
            rows = [[('↗️ Поделиться', share)], [('📋 Скопировать ссылку', 'copy:'+link)],
                    [('📋 Скопировать готовый текст', 'copy:'+message+'\n\n'+link)],
                    [('Остановить новые активации', f'{ADMIN}:close:{token}')]]
            body += '\n\n<code>' + escape(link) + '</code>\n\nЭту ссылку можно добавить в кнопку рассылки.'
        return rich(body, rows + [[('← Рекламные ссылки', ADMIN)]])

    async def reserve(self, token, user_id):
        """Pin server and expiry before any provider call, releasing the campaign lock first."""
        key = f'{RESERVATION}{token}:{user_id}'
        async with durable_session(self.repo) as session, session.begin():
            row = await session.get(AppSetting, key)
            if row:
                data = json.loads(row.value)
                may_move = not data['attempted']
                if may_move:
                    data['attempted'] = True
                    row.value = json.dumps(data)
                return data, may_move
            campaign = await session.get(AppSetting, campaign_key(token), with_for_update=True)
            if not campaign:
                raise ValueError('Рекламная ссылка не найдена.')
            data = json.loads(campaign.value)
            if data['state'] == 'closed':
                raise ValueError('Рекламная ссылка остановлена.')
            if status(data) != 'Активна':
                raise ValueError('Рекламная ссылка больше не действует.')
            servers = await ProxyService(SQLRepository(session), self.settings).matching_servers('paid')
            if not servers:
                raise CampaignRetry('Сейчас нет свободного прокси-сервера. Пожалуйста, попробуйте позже.')
            saved = dict(ip=data['ip'], end=(now()+timedelta(days=data['days'])).isoformat(),
                         **placement(servers[0], data['ip']))
            session.add(AppSetting(key=key, value=json.dumps(saved)))
            return saved, True

    async def relocate(self, token, user_id, data, excluded):
        """Only called after a preflight rejection which did not reach Telemt."""
        service = ProxyService(self.repo, self.settings)
        alternative = next((s for s in await service.matching_servers('paid') if s.id not in excluded), None)
        moved = dict(data, **placement(alternative, data['ip'])) if alternative else dict(data, attempted=False)
        key = f'{RESERVATION}{token}:{user_id}'
        async with durable_session(self.repo) as session, session.begin():
            row = await session.get(AppSetting, key, with_for_update=True)
            row.value = json.dumps(moved)
        return moved if alternative else None

    async def claim(self, token, user):
        campaign_key(token)
        user = await self.session.get(User, user.id, with_for_update=True, populate_existing=True)
        if not user or user.status != 'active' or not user.telegram_id or user.telegram_id <= 0:
            raise ValueError('Аккаунт недоступен.')
        done_key = f'{CLAIM}{token}:{user.id}'
        done = await self.session.get(AppSetting, done_key, populate_existing=True)
        service = ProxyService(self.repo, self.settings)
        if done:
            credential = await self.session.get(ProxyCredential, int(done.value))
            return self.receipt(service, credential, user)

        data, may_move = await self.reserve(token, user.id)
        end = datetime.fromisoformat(data['end'])
        if end <= now():
            raise ValueError('Срок прокси по этой ссылке закончился.')
        excluded = set()
        operation = 'campaign_' + hashlib.sha256(token.encode()).hexdigest()[:20]
        while data:
            excluded.add(data['server'])
            try:
                async with self.session.begin_nested():
                    server = await self.session.get(ProxyServer, data['server'])
                    if not server:
                        raise ProxyPlacementError('Сервер не найден.')
                    credential, _ = await service.provision(server, user, end, data['ip'], data['tcp'],
                                                             'paid', connection_token=operation)
                    from app.services.proxy_control import meta
                    detail = await meta(self.repo, credential)
                    detail.source = 'admin'
                    detail.ip_limit = data['ip']
                    screen = self.receipt(service, credential, user)
                    await self.repo.add(AppSetting(key=done_key, value=str(credential.id)))
                return screen
            except ProxyPlacementError:
                if not may_move:
                    break
                data = await self.relocate(token, user.id, data, excluded)
            except ValueError as error:
                raise CampaignRetry('Пока не удалось подтвердить выдачу. Пожалуйста, попробуйте ещё раз.') from error
        raise CampaignRetry('Прокси-сервер временно недоступен. Пожалуйста, попробуйте позже.')

    @staticmethod
    def receipt(service, credential, user):
        if (not credential or credential.user_id != user.id or not credential.link
                or credential.status != 'active' or utc(credential.expires_at) <= now()):
            raise ValueError('Прокси по этой ссылке больше не активен. Проверьте раздел «Мои прокси».')
        target = 'url:' + service.public_link(service.decrypt(credential.link))
        end = utc(credential.expires_at).astimezone(MOSCOW).strftime('%d.%m.%Y')
        screen = rich(f'🎁 <b>Ваш Telegram Proxy готов</b>\n\nДоступ до <b>{end}</b>. '
                      'Нажмите кнопку ниже и подтвердите подключение в Telegram.',
                      [[('✈️ Подключить прокси', target)], [('Мои прокси', 'p:home')]])
        screen.button_options[target] = {'style': 'primary'}
        return screen
