import json
from calendar import monthrange
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from aiogram.types import InputRichBlockButtons, InputRichMessage
from sqlalchemy import text

from app.database.models import now
from app.services.screens import money as format_money
from app.services.bot_catalog import rich_table
from app.web.admin_reports import SALES, TZ, build_report, period_bounds

MONTHS = ('Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
          'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь')
VIEWS = ('day', 'vpn', 'antig')


def obj(raw):
    if isinstance(raw, dict): return raw
    return json.loads(raw or '{}')


def money(amount):
    return format_money(amount).replace(".", ",")


def diff(current, previous):
    if not previous:
        return '0%' if not current else 'с нуля'
    change = (Decimal(current - previous) * 100 / previous).quantize(Decimal('0.1'))
    return (f'{change:+g}%' if change else '0%').replace('.', ',').replace('-', '−')


def classify_basic(row):
    snap, order, addon = map(obj, (row['snap'], row['ord'], row['addon']))
    if row['kind'] != 'vpn':
        return 'other', None
    if snap.get('antig_addon') or snap.get('sale_category') == 'antig_addon':
        if row['months'] and snap.get('sale_category') != 'antig_addon':
            base = addon.get('base')
            return 'bundle', row['amount'] - base if type(base) is int and 0 <= base <= row['amount'] else None
        return 'addon', row['amount']
    extra = snap.get('antig_surcharge', order.get('antig_surcharge'))
    profile = snap.get('bot_test_access') or order.get('bot_test_access') or {}
    if type(extra) is int and extra > 0:
        return 'bundle', extra if extra <= row['amount'] else None
    if len(profile.get('squads', ())) == 2:
        return 'bundle', 0 if extra == 0 else None
    if extra == 0 or len(profile.get('squads', ())) == 1:
        return 'standard', None
    if row['status'] in ('order_paid','stars_paid') and order and not row['renewal']:
        return 'standard', None
    return 'unknown', None




def classify(row):
    kind, extra = classify_basic(row)
    if kind != 'unknown':
        return kind, extra
    grant, addon, creation = map(obj, (row['grant_data'], row['addon'], row['creation']))
    if len((grant.get('widget_access') or {}).get('squads', [])) == 2:
        return 'bundle', None
    if addon.get('antig') is True:
        base = addon.get('base_total')
        return 'bundle', row['amount'] - base if type(base) is int and 0 <= base <= row['amount'] else None
    if grant or (not row['renewal'] and len(creation.get('activeInternalSquads', [])) == 1):
        return 'standard', None
    if not row['renewal'] and len(creation.get('activeInternalSquads', [])) == 2:
        return 'bundle', None
    return 'unknown', None


async def sales_breakdown(session, lo, hi):
    params=dict(lo=lo,hi=hi,kind='all',source='all',months=-1,devices=0,channel='all',sale_type='all')
    rows=(await session.execute(text(SALES+"""
    SELECT s.kind,s.months,s.devices,s.renewal,s.amount,s.dt,p.status,
      ss.value snap, coalesce(o.value,so.value) ord, coalesce(a.value,b.value,c.value) addon,
      f.value current_profile, g.value grant_data, vc.request creation
    FROM selected s
    LEFT JOIN payments p ON p.id = cast(nullif(regexp_replace(s.id,'[^0-9]','','g'),'') AS bigint) AND s.id NOT LIKE 'B%'
    LEFT JOIN app_settings ss ON ss.key='sale_snapshot:'||s.id
    LEFT JOIN app_settings o ON o.key='order:'||s.id
    LEFT JOIN app_settings so ON so.key='stars_order:'||s.id
    LEFT JOIN app_settings a ON p.provider_id LIKE 'bot-antig:%' AND a.key='bot_antig_addon:'||split_part(p.provider_id,':',2)
    LEFT JOIN app_settings b ON p.provider_id LIKE 'bot-antig:%' AND b.key='web_key_antig:'||split_part(p.provider_id,':',2)
    LEFT JOIN app_settings c ON p.provider_id LIKE 'bot-custom-renew:%' AND c.key='admin_custom_renew:'||split_part(p.provider_id,':',2)
    LEFT JOIN app_settings g ON (p.provider_id LIKE 'bot-custom-sale:%' OR p.provider_id LIKE 'bot-client-sale:%')
      AND g.key='client_grant:'||split_part(p.provider_id,':',2)
    LEFT JOIN vless_creations vc ON vc.subscription_id=s.subscription_id AND abs(extract(epoch from (vc.created_at-s.dt)))<300
    LEFT JOIN app_settings f ON f.key='bot_test_access:'||s.subscription_id
    """),params)).mappings().all()

    counts, amounts = Counter(), Counter()
    bundle_extra, unpriced = 0, 0
    for row in rows:
        group, extra = classify(row)
        counts[group] += 1
        amounts[group] += row['amount']
        if group == 'bundle':
            if extra is None:
                unpriced += 1
            else:
                bundle_extra += extra
    return counts, amounts, bundle_extra, unpriced


def buttons_grid(items, columns):
    return [InputRichBlockButtons(buttons=items[i:i+columns])
            for i in range(0, len(items), columns)]


async def render_calendar(repo, action, earliest, button):
    today = now().astimezone(TZ).date()
    parts = action.split(':')
    screen = parts[0]
    if screen in ('months', 'dates'):
        if len(parts) not in (3, 4) or parts[1] not in VIEWS:
            raise ValueError('Invalid calendar')
        view = parts[1]
        mode = parts[3] if len(parts) == 4 else ''
        begin = date.fromisoformat(mode) if mode and mode != 'start' else None
        if begin and not earliest <= begin <= today:
            raise ValueError('Invalid start date')
        minimum = begin or earliest
        suffix = ':' + mode if mode else ''
        caption = 'начала' if mode == 'start' else 'окончания' if begin else ''
        if screen == 'months':
            year = int(parts[2])
            if not minimum.year <= year <= today.year:
                raise ValueError('Invalid year')
            months = (month for month in range(1, 13)
                      if (minimum.year, minimum.month) <= (year, month) <= (today.year, today.month))
            items = [button(f'dates:{view}:{year:04d}-{month:02d}{suffix}', MONTHS[month-1])
                     for month in months]
            nav = [button(f'months:{view}:{other}{suffix}', str(other), style='link')
                   for other in (year-1, year+1) if minimum.year <= other <= today.year]
            heading = ' '.join(part for part in ('Выберите месяц', caption, f'· {year}') if part)
            grid = buttons_grid(items, 3)+buttons_grid(nav, 2)
        else:
            month = date.fromisoformat(parts[2]+'-01')
            start = max(minimum, month)
            last = month.replace(day=monthrange(month.year,month.month)[1])
            end = min(today, last)
            if end < start:
                raise ValueError('Invalid month')
            days = [start+timedelta(days=i) for i in range((end-start).days+1)]
            if mode == 'start':
                targets = [f'months:{view}:{day.year}:{day.isoformat()}' for day in days]
            elif begin:
                targets = [f'{view}:{begin.isoformat()}:{day.isoformat()}' for day in days]
            else:
                targets = [f'{view}:{day.isoformat()}' for day in days]
            items = [button(target, str(day.day)) for target, day in zip(targets, days)]
            heading = ' '.join(part for part in ('Выберите день', caption, f'· {MONTHS[month.month-1]} {month.year}') if part)
            grid = []
            if not mode:
                month_label = 'Весь месяц' if end == last else 'Месяц по сегодня'
                grid = buttons_grid([button(f'{view}:{start.isoformat()}:{end.isoformat()}', month_label)], 1)
            grid += buttons_grid(items, 6)+buttons_grid([
                button(f'months:{view}:{month.year}{suffix}', 'К выбору месяца', style='link')], 1)
        blocks = [{'type':'heading','text':heading,'size':2}, *grid]
        blocks += buttons_grid([button(view, 'Сегодня', style='link')], 1)
        return InputRichMessage(blocks=blocks,skip_entity_detection=True)

    if screen not in VIEWS or len(parts)>3:
        raise ValueError('Invalid report')
    day = date.fromisoformat(parts[1]) if len(parts)>=2 else today
    end = date.fromisoformat(parts[2]) if len(parts)==3 else day
    if not earliest <= day <= end <= today:
        raise ValueError('Invalid day or range')
    selection = day.isoformat() if day == end else f'{day.isoformat()}:{end.isoformat()}'
    lo, hi = period_bounds('custom',day.isoformat(),end.isoformat())
    date_label = day.strftime('%d.%m.%Y') if day == end else f'{day:%d.%m.%Y} – {end:%d.%m.%Y}'
    if end == today:
        intro = 'Данные на '+now().astimezone(TZ).strftime('%H:%M')+' МСК'
    else:
        intro = 'Московское время'
    if screen == 'day':
        report=(await build_report(repo.session,lo,hi,limit=1))['stats']
        rows = [
            [label, money(report[field]) if field in ('revenue','average') else str(report[field]),
             ('—' if report[field+'_growth'] is None else (f'{report[field+"_growth"]:+g}%'.replace('.',',') if report[field+'_growth'] else '0%'))]
            for label,field in [('Выручка','revenue'),('Продажи','sales'),('Регистрации','new_customers'),
                                ('Покупатели','buyers'),('Продления','renewals'),('Средний чек','average')]
        ]
        comparison = ('К '+(day-timedelta(days=1)).strftime('%d.%m')) if day == end else 'К пред. периоду'
        result=rich_table('📊 Итоги '+date_label,intro,['Показатель','Итог',comparison],rows)
        links=[button(f'vpn:{selection}', 'Продажи VPN'),button(f'antig:{selection}', 'Выручка от антиглушилки')]
    else:
        counts, amounts, bundle_extra, unpriced = await sales_breakdown(repo.session,lo,hi)
        if screen == 'antig':
            rows=[
                ['Подписки с антиглушилкой',str(counts['bundle']),money(amounts['bundle'])],
                ['Из них наценка','—',money(bundle_extra)],
                ['К действующим ключам',str(counts['addon']),money(amounts['addon'])],
                ['Всего доплат','—',money(bundle_extra+amounts['addon'])],
            ]
            note=f'Наценка не указана в {unpriced} продажах.' if unpriced else ''
            result=rich_table('Выручка от антиглушилки · '+date_label,intro,['Показатель','Кол-во','Сумма'],rows,note)
        else:
            rows=[[label,str(counts[key]),money(amounts[key])] for label,key in
                  [('VPN с антиглушилкой','bundle'),('Стандартный VPN','standard'),
                   ('Доплата к действующему ключу','addon'),('VPN без отметки о дополнении','unknown')]
                  if key in ('bundle','standard') or counts[key]]
            result=rich_table('Продажи VPN · '+date_label,intro,['Категория','Продажи','Выручка'],rows)
        links=[button(f'day:{selection}','К сводке',style='link')]
    links.append(button(f'months:{screen}:{day.year}','Выбрать дату'))
    links.append(button(f'months:{screen}:{day.year}:start','Выбрать период'))
    if day != today or end != today:
        links.append(button(screen,'Сегодня',style='link'))
    return InputRichMessage(blocks=[*result.blocks,*buttons_grid(links,1)],skip_entity_detection=True)
