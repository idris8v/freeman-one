"""Telegram wholesale receivables, separate from customer wallet and paid orders."""
import json
import re
import secrets
from datetime import date, timedelta
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.database.models import AdminAudit, AppSetting, ChatNotice, User, now
from app.services.admin_client_grant import rich
from app.services.screens import duration, money
from app.services.vless_creation import utc


MOSCOW = ZoneInfo('Europe/Moscow')
LEDGER = 'partner_ledger:'
ACCOUNT = 'partner_ledger_account:'
PENDING = 'partner_ledger_draft:'
LABELS = {'issue': 'Выдача VPN', 'antig': 'Антиглушилка', 'payment': 'Оплата',
          'adjust': 'Корректировка', 'opening': 'Начальный долг'}
PRICE = {
    1: {1: 9000, 3: 21000, 5: 35000},
    3: {1: 25000, 3: 65000, 5: 90000},
    6: {1: 45000, 3: 95000, 5: 140000},
    12: {1: 65000, 2: 110000, 3: 150000, 5: 250000},
}
PAGE = 6


def local_day():
    return now().astimezone(MOSCOW).date()


def date_label(day):
    return date.fromisoformat(day).strftime('%d.%m.%Y')


async def lock(repo, uid):
    return await repo.session.scalar(select(User.id).where(User.id == uid).with_for_update())


async def entries(repo, uid):
    values = await repo.session.scalars(select(AppSetting.value).where(AppSetting.key.startswith(f'{LEDGER}{uid}:')))
    records = [json.loads(value) for value in values]
    return sorted(records, key=lambda record: (record['effective_at'], record['recorded_at']), reverse=True)


def total(records):
    return sum(record['amount'] for record in records)


async def append(repo, uid, ref, kind, amount, actor, *, effective_at=None, **details):
    """Call while holding the partner row lock, in the same transaction as fulfillment."""
    key = f'{LEDGER}{uid}:{ref}'
    existing = await repo.session.get(AppSetting, key, populate_existing=True)
    if existing:
        record = json.loads(existing.value)
        if (record['kind'], record['amount'], record['actor']) != (kind, amount, actor):
            raise ValueError('Эта операция уже записана с другими условиями.')
        return record
    stamp = now()
    record = dict(kind=kind, amount=amount, actor=actor, recorded_at=stamp.isoformat(),
                  effective_at=effective_at or stamp.astimezone(MOSCOW).date().isoformat(), **details)
    await repo.add(AppSetting(key=key, value=json.dumps(record, ensure_ascii=False)))
    if not await repo.setting(f'{ACCOUNT}{uid}'):
        await repo.set_setting(f'{ACCOUNT}{uid}', stamp.isoformat())
    return record


async def partners(repo):
    roles = await repo.settings_prefix('staff_role:')
    ids = [int(key.split(':')[1]) for key, role in roles.items()
           if role == 'partner' and key.split(':')[1].isdigit()]
    return list(await repo.session.scalars(select(User).where(User.telegram_id.in_(ids)).order_by(User.id)))


async def historical(repo, user):
    """Reconcile immutable issuance snapshots. Current ownership and tariff are mutable."""
    audit = list(await repo.session.scalars(select(AdminAudit).where(
        AdminAudit.action == 'admin_free', AdminAudit.actor_telegram_id == user.telegram_id,
        AdminAudit.details.startswith('Бесплатная выдача сотруднику'),
    ).order_by(AdminAudit.created_at, AdminAudit.id)))
    role_changes = list(await repo.session.scalars(select(AdminAudit).where(
        AdminAudit.target == str(user.telegram_id), AdminAudit.action.in_(('staff_role', 'staff_remove')),
    ).order_by(AdminAudit.created_at, AdminAudit.id)))
    originals = {}
    for raw in (await repo.settings_prefix('admin_grant:')).values():
        try:
            grant = json.loads(raw)
        except ValueError:
            continue
        if (isinstance(grant, dict) and grant.get('actor') == user.telegram_id
                and grant.get('sid') == 0 and type(grant.get('result')) is int and grant['result'] > 0):
            originals[str(grant['result'])] = grant.get('sale_terms') or {}
    result = {}
    change_index = 0
    role = ''
    for event in audit:
        while change_index < len(role_changes):
            change = role_changes[change_index]
            if (utc(change.created_at), change.id) > (utc(event.created_at), event.id):
                break
            role = change.details if change.action == 'staff_role' else ''
            change_index += 1
        if role and role != 'partner':
            continue
        terms = originals.get(event.target, {})
        price = PRICE.get(terms.get('months'), {}).get(terms.get('devices')) if role == 'partner' else None
        if (terms.get('bot_test_access') or {}).get('squads'):
            price = None
        result.setdefault(event.target, dict(
            sid=event.target, day=utc(event.created_at).astimezone(MOSCOW).date().isoformat(),
            months=terms.get('months'), devices=terms.get('devices'), amount=price,
        ))
    return list(reversed(list(result.values())))


def statement(records, offset, root, back, *, monthly=False, actors=None):
    debt = total(records)
    last = next((row['effective_at'] for row in records if row['kind'] == 'payment'), None)
    header = f'💼 <b>Расчёты</b>\n\nТекущий долг <b>{money(debt)}</b>'
    if last:
        header += f'\nПоследняя оплата {date_label(last)}'
    paid = sum(-row['amount'] for row in records if row['amount'] < 0)
    for row in reversed(records):
        if row['amount'] <= 0:
            continue
        if row['amount'] > paid:
            header += f'\nСамая ранняя открытая сумма от {date_label(row["effective_at"])}'
            break
        paid -= row['amount']
    blocks = []
    visible = records
    if monthly:
        month = local_day().isoformat()[:7]
        visible = [row for row in records if row['effective_at'].startswith(month)]
        header += f'\nВыписка за {local_day():%m.%Y}'
    for row in visible[offset:offset + PAGE]:
        amount = ('+' if row['amount'] > 0 else '−' if row['amount'] < 0 else '') + money(abs(row['amount']))
        label = LABELS[row['kind']]
        if row.get('sid'):
            label += f' №{row["sid"]}'
        if row.get('months') and row.get('devices'):
            label += f' · {duration(row["months"])} · {row["devices"]} устр.'
        line = f'<b>{date_label(row["effective_at"])} · {amount}</b>\n{label}'
        if row.get('antig') and row['kind'] == 'issue':
            line += f'\nVPN {money(row["base"])} · Антиглушилка {money(row["antig"])}'
        if row.get('note'):
            line += '\n' + escape(row['note'])
        if actors and row['actor'] in actors and row['kind'] in ('payment','adjust','opening'):
            line += '\nЗаписал: '+escape(actors[row['actor']])
        blocks.append(line)
    rows = page_buttons(root+':month' if monthly else root, offset, len(visible))
    rows.append([('Все операции',root) if monthly else ('🗓 Выписка за месяц',root+':month')])
    return rich(header + '\n\n' + ('\n\n'.join(blocks) or 'Операций пока нет.'), rows + [[('← Назад', back)]])


def page_buttons(root, offset, count):
    rows = []
    if offset:
        rows.append([('← Более новые', f'{root}:{max(0, offset-PAGE)}')])
    if offset + PAGE < count:
        rows.append([('Более ранние →', f'{root}:{offset+PAGE}')])
    return rows


class PartnerLedger:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings

    async def require(self, actor):
        if not self.settings.is_owner(actor.telegram_id) or actor.status != 'active':
            raise PermissionError('Расчёты доступны администратору.')
        if actor.telegram_id not in self.settings.owner_ids():
            if (await self.repo.setting(f'staff_role:{actor.telegram_id}') != 'administrator'
                    or await self.repo.setting(f'staff_blocked:{actor.telegram_id}') == '1'):
                raise PermissionError('Доступ закрыт.')

    async def customer(self, uid):
        user = await self.repo.session.get(User, uid)
        if not user:
            raise ValueError('Партнёр не найден.')
        if await self.repo.setting(f'staff_role:{user.telegram_id}') != 'partner' and not await self.repo.setting(f'{ACCOUNT}{uid}'):
            raise ValueError('Партнёрский счёт не найден.')
        return user

    async def render(self, action, actor):
        await self.require(actor)
        p = action.split(':')
        if len(p) == 2:
            current = {u.id: u for u in await partners(self.repo)}
            for key in await self.repo.settings_prefix(ACCOUNT):
                uid = int(key.split(':')[1])
                if uid not in current:
                    user = await self.repo.session.get(User, uid)
                    if user:
                        current[uid] = user
            rows = []
            for user in current.values():
                debt = total(await entries(self.repo, user.id))
                rows.append([(f'{user.display_name[:38]} · {money(debt)}', f'admin:partnerdebt:view:{user.id}')])
            return rich('🤝 <b>Расчёты с партнёрами</b>\n\nВыберите партнёра.', rows + [[('← Назад', 'admin:commerce')]])
        if p[2] == 'apply':
            return await self.apply(p[3], actor)
        if p[2] == 'full':
            user = await self.customer(int(p[3]))
            amount = total(await entries(self.repo, user.id))
            if amount <= 0:
                raise ValueError('У партнёра нет долга.')
            return await self.preview(user, 'payment', -amount, local_day(), '', actor, full=True)
        user = await self.customer(int(p[3]))
        monthly = len(p) > 4 and p[4] == 'month'
        offset_index = 5 if monthly else 4
        offset = max(0, int(p[offset_index])) if len(p) > offset_index else 0
        if p[2] == 'history':
            return await self.history_screen(user, offset)
        if p[2] != 'view':
            raise ValueError('Действие недоступно.')
        records = await entries(self.repo, user.id)
        actor_ids = {row['actor'] for row in records if row['kind'] in ('payment','adjust','opening')}
        actors = {u.id:u.display_name for u in await self.repo.session.scalars(select(User).where(User.id.in_(actor_ids)))} if actor_ids else {}
        result = statement(records, offset, f'admin:partnerdebt:view:{user.id}', 'admin:partnerdebt',
                           monthly=monthly, actors=actors)
        if offset == 0 and not monthly:
            actions = [[('📋 Сверка старых выдач', f'admin:partnerdebt:history:{user.id}')],
                       [('✏️ Скорректировать долг', f'admin:input:partnerdebt:adjust:{user.id}')]]
            if total(records) > 0:
                actions[:0] = [[('💵 Записать оплату', f'admin:input:partnerdebt:payment:{user.id}')],
                               [('✅ Записать полное погашение', f'admin:partnerdebt:full:{user.id}')]]
            if not any(row['kind'] == 'opening' for row in records):
                actions.append([('➕ Внести начальный долг', f'admin:input:partnerdebt:opening:{user.id}')])
            result.rows[-1:-1] = actions
        return rich(f'<b>{escape(user.display_name)}</b>\n\n'+result.text, result.rows)

    async def history_screen(self, user, offset):
        records = await historical(self.repo, user)
        known = sum(row['amount'] or 0 for row in records)
        lines = []
        for row in records[offset:offset + PAGE]:
            terms = f' · {row["devices"]} устр. · {duration(row["months"])}' if row['devices'] and row['months'] else ''
            amount = money(row['amount']) if row['amount'] is not None else 'требует сверки'
            lines.append(f'{date_label(row["day"])} · №{escape(row["sid"])}{terms} · {amount}')
        body = (f'📋 <b>Сверка старых выдач</b>\n\nВыдач: {len(records)}\n'
                f'По известным тарифам: {money(known)}\n'
                f'Требуют ручной проверки: {sum(row["amount"] is None for row in records)}\n\n'
                'Суммы приведены для сверки. Запишите согласованный остаток через «Внести начальный долг».')
        if lines:
            body += '\n\n' + '\n'.join(lines)
        rows = page_buttons(f'admin:partnerdebt:history:{user.id}', offset, len(records))
        return rich(body, rows + [[('← Назад', f'admin:partnerdebt:view:{user.id}')]])

    async def prompt(self, action, actor):
        await self.require(actor)
        _, _, _, kind, uid = action.split(':')
        user = await self.customer(int(uid))
        if kind not in ('payment', 'adjust', 'opening'):
            raise ValueError('Действие недоступно.')
        example = '-100 | 2026-11-09 | Исправление начисления' if kind == 'adjust' else '500 | 2026-11-09 | Перевод'
        screen = rich(f'<b>{LABELS[kind]} · {escape(user.display_name)}</b>\n\n'
                      f'Введите сумму в рублях, дату ГГГГ-ММ-ДД и комментарий через |.\n\n'
                      f'Например: <code>{example}</code>\n\nДата по умолчанию — сегодня. Для корректировки укажите причину.',
                      [[('← Назад', f'admin:partnerdebt:view:{uid}')]])
        screen.admin_input_action = action
        return screen

    async def input(self, action, text, actor):
        await self.require(actor)
        _, _, _, kind, uid = action.split(':')
        user = await self.customer(int(uid))
        raw, *rest = [part.strip() for part in text.split('|', 2)]
        match = re.fullmatch(r'([+-]?)([0-9]{1,7})(?:[.,]([0-9]{1,2}))?', raw)
        if not match or kind not in ('payment', 'adjust', 'opening'):
            raise ValueError('Введите сумму в рублях, например 500 или 500,50.')
        amount = int(match[2])*100 + int((match[3] or '').ljust(2, '0'))
        if match[1] == '-':
            amount = -amount
        note = rest[1] if len(rest) == 2 else ''
        try:
            day = date.fromisoformat(rest[0]) if rest and rest[0] else local_day()
        except ValueError:
            raise ValueError('Укажите дату в формате ГГГГ-ММ-ДД.') from None
        if day > local_day() or day.year < 2020:
            raise ValueError('Укажите прошедшую дату или сегодня.')
        if not amount or (kind != 'adjust' and amount < 0) or len(note) > 200 or (kind == 'adjust' and not note):
            raise ValueError('Проверьте сумму и комментарий. Для корректировки укажите причину до 200 символов.')
        return await self.preview(user, kind, -amount if kind == 'payment' else amount, day, note, actor)

    async def preview(self, user, kind, amount, day, note, actor, *, full=False):
        token = secrets.token_hex(10)
        debt = total(await entries(self.repo, user.id))
        if debt + amount < 0:
            raise ValueError('Сумма превышает текущий долг.')
        await self.repo.set_setting(PENDING+token, json.dumps(dict(
            uid=user.id, actor=actor.id, amount=amount, day=day.isoformat(), kind=kind, note=note,
            full=full, until=now().timestamp()+900,
        )))
        signed = ('+' if amount>0 else '−') + money(abs(amount))
        return rich(f'<b>{LABELS[kind]} · {escape(user.display_name)}</b>\n\n'
                    f'Изменение долга <b>{signed}</b>\nДата {date_label(day.isoformat())}\n'
                    f'Долг после операции <b>{money(debt+amount)}</b>'
                    + (f'\n\n{escape(note)}' if note else ''),
                    [[('✅ Подтвердить запись', f'admin:partnerdebt:apply:{token}')],
                     [('← Назад', f'admin:partnerdebt:view:{user.id}')]])

    async def apply(self, token, actor):
        if not re.fullmatch(r'[0-9a-f]{20}', token):
            raise ValueError('Подтверждение недоступно.')
        draft = json.loads(await self.repo.setting(PENDING+token, '{}'))
        if draft.get('actor') != actor.id:
            raise PermissionError('Подтверждение другого администратора.')
        uid = draft['uid']
        await self.customer(uid)
        await lock(self.repo, uid)
        ref = f'admin:{token}'
        if not await self.repo.session.get(AppSetting, f'{LEDGER}{uid}:{ref}', populate_existing=True):
            records = await entries(self.repo, uid)
            debt, amount = total(records), draft['amount']
            if now().timestamp() > draft['until']:
                raise ValueError('Подтверждение устарело. Откройте расчёты заново.')
            if debt + amount < 0 or (draft['full'] and debt != -amount):
                raise ValueError('Долг изменился. Проверьте сумму в расчётах.')
            if draft['kind'] == 'opening' and any(row['kind'] == 'opening' for row in records):
                raise ValueError('Начальный долг уже внесён. Используйте корректировку.')
            await append(self.repo, uid, ref, draft['kind'], amount, actor.id,
                         effective_at=draft['day'], note=draft['note'])
        return await self.render(f'admin:partnerdebt:view:{uid}', actor)


async def reminder_screen(repo, user):
    if (not user or user.status != 'active' or await repo.setting(f'staff_role:{user.telegram_id}') != 'partner'
            or await repo.setting(f'staff_blocked:{user.telegram_id}') == '1'
            or await repo.setting(f'notifications:{user.id}', '1') != '1'):
        return None
    debt = total(await entries(repo, user.id))
    if debt <= 0:
        return None
    return rich(f'🧾 <b>Расчёт за неделю</b>\n\nТекущий долг <b>{money(debt)}</b>. '
                'Выдачи и учтённые оплаты доступны в разделе «Расчёты».', [[('Открыть расчёты', 'partner:ledger')]])


async def prepare_reminders(repo):
    local = now().astimezone(MOSCOW)
    if local.weekday() != 0 or local.hour < 12:
        return
    for user in await partners(repo):
        dedup = f'partner.debt:{user.id}:{local.date().isoformat()}'
        if not await repo.session.scalar(select(ChatNotice.id).where(ChatNotice.dedup == dedup)):
            if await reminder_screen(repo, user):
                await repo.add(ChatNotice(dedup=dedup, target_id=user.telegram_id,
                                          kind='partner_debt', text='Расчёт за неделю'))
