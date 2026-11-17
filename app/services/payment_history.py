"""Read-only payment journal and matching CSV/Excel exports."""
import io
from datetime import timedelta
from decimal import Decimal
from html import escape
from sqlalchemy import select,func
from aiogram.types import InputRichMessage
from openpyxl import Workbook
from openpyxl.styles import Font,PatternFill
from openpyxl.worksheet.table import Table,TableStyleInfo
from app.database.models import Payment,User,now
from app.services.screens import Screen,money
from app.services.accounts import display_freeman_id
from app.services.reporting import csv_file,safe_cell

NAMES = {'gift_created':'подарок: счёт создан','gift_pending':'подарок: ожидает оплаты','gift_canceled':'подарок: оплата отменена','manual_paid':'ручная продажа оплачена','manual_unpaid':'ручная продажа не оплачена','manual_preparing':'ручная выдача готовится','manual_received_pending':'оплата получена, выдача проверяется','topup_created':'пополнение создано','topup_pending':'пополнение ожидает','topup_credited':'пополнение зачислено','topup_canceled':'пополнение отменено','order_created':'заказ создан','order_pending':'заказ ожидает','order_processing':'заказ обрабатывается','order_paid':'подписка оплачена','order_canceled':'заказ отменён','proxy_order_created':'заказ прокси создан','proxy_order_pending':'прокси ожидает оплаты','proxy_order_processing':'прокси активируется','proxy_order_paid':'прокси оплачен','proxy_order_canceled':'заказ прокси отменён','balance_paid':'оплата с баланса','stars_created':'счёт Stars создан','stars_pending':'Stars ожидаются','stars_processing':'Stars обрабатываются','stars_paid':'оплачено Stars'}
PAID=('succeeded','order_paid','proxy_order_paid','stars_paid','balance_paid','topup_credited','manual_paid')
PERIODS={0:'Всё время',1:'24 часа',7:'7 дней',30:'30 дней',90:'90 дней'}
HEADERS=['ID','Дата UTC','Freeman ID','Имя','Telegram ID','Операция','Статус','Сумма, ₽']

def operation(status):
    return 'Пополнение' if status.startswith('topup_') else 'Прокси' if status.startswith('proxy_') else 'Оплата с баланса' if status=='balance_paid' else 'Подписка / платёж'

def xlsx_file(rows):
    workbook=Workbook();sheet=workbook.active;sheet.title='Платежи'
    sheet.append(HEADERS)
    for row in rows:sheet.append([safe_cell(cell) for cell in row])
    sheet.freeze_panes='A2'
    for cell in sheet[1]:cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='1677FF')
    widths=[12,25,20,30,20,26,32,18]
    for column,width in zip('ABCDEFGH',widths):sheet.column_dimensions[column].width=width
    for cells in sheet.iter_rows(min_row=2,min_col=8,max_col=8):cells[0].number_format='#,##0.00'
    if rows:
        table=Table(displayName='Payments',ref=f'A1:H{len(rows)+1}')
        table.tableStyleInfo=TableStyleInfo(name='TableStyleMedium2',showRowStripes=True)
        sheet.add_table(table)
    output=io.BytesIO();workbook.save(output);return output.getvalue()

async def render_payments(repo,parts):
    offset=max(0,int(parts[2])) if len(parts)>2 else 0
    days=int(parts[3]) if len(parts)>3 else 0
    if days not in PERIODS:raise ValueError('Период недоступен.')
    export=parts[4] if len(parts)>4 else None
    condition=[]
    if days:condition.append(Payment.created_at>=now()-timedelta(days=days))
    query=select(Payment,User).outerjoin(User,User.id==Payment.user_id).where(*condition).order_by(Payment.id.desc())
    back=f'admin:topups:{offset}:{days}'
    if export:
        if export not in ('csv','xlsx'):raise ValueError('Формат недоступен.')
        data=(await repo.session.execute(query)).all()
        rows=[[pay.id,pay.created_at.strftime('%Y-%m-%d %H:%M:%S'),display_freeman_id(user.freeman_id) if user else '',user.display_name if user else '',user.telegram_id if user else '',operation(pay.status),NAMES.get(pay.status,pay.status),Decimal(pay.amount)/100] for pay,user in data]
        content=csv_file(HEADERS,rows) if export=='csv' else xlsx_file(rows)
        title=f'📤 Выгрузка платежей · {PERIODS[days]}'
        return Screen(title+f'\n\nЗаписей: {len(rows)}. Даты указаны в UTC.',[[('← К платежам',back)]],document=(content,f'freeman-payments-{days}d.{export}'),structured=True)
    count=await repo.session.scalar(select(func.count()).select_from(Payment).where(*condition)) or 0
    data=(await repo.session.execute(query.offset(offset).limit(6))).all()
    async def total(statuses):
        return await repo.session.scalar(select(func.coalesce(func.sum(Payment.amount),0)).where(*condition,Payment.status.in_(statuses))) or 0
    external=await total(('succeeded','order_paid','proxy_order_paid','stars_paid','manual_paid'))
    topups=await total(('topup_credited',));wallet=await total(('balance_paid',));refunds=await total(('refunded',))
    summary=f'📅 {PERIODS[days]} · записей: {count}\n\n💳 Оплаченные покупки: {money(external)}\n💰 Зачисленные пополнения: {money(topups)}\n👛 Покупки с баланса: {money(wallet)}\n↩️ Возвраты: {money(refunds)}\n\nПокупки с баланса показаны отдельно: повторно считать их денежным поступлением нельзя.'
    blocks=[dict(type='heading',size=1,text='💵 Платежи и пополнения'),dict(type='paragraph',text=summary),dict(type='divider')]
    def cell(text,header=False,align='left'):
        return dict(text=str(text),is_header=header,align=align,valign='middle')
    totals=[[cell('Показатель',True),cell('Сумма',True,'right')]]
    totals += [[cell(label),cell(money(value),align='right')] for label,value in [('Оплаченные покупки',external),('Пополнения',topups),('Покупки с баланса',wallet),('Возвраты',refunds)]]
    blocks=[dict(type='heading',size=1,text='💵 Платежи и пополнения'),dict(type='paragraph',text=f'📅 {PERIODS[days]} · записей: {count}'),dict(type='table',cells=totals,is_bordered=True,is_striped=True,is_compact=True,caption='Сводка за выбранный период'),dict(type='paragraph',text='Покупки с баланса показаны отдельно и повторно не считаются денежным поступлением.'),dict(type='divider')]
    cells=[[cell(label,True) for label in ('№ / Дата UTC','Клиент','Сумма','Операция / Статус')]]
    text=summary
    for pay,user in data[:5]:
        label=display_freeman_id(user.freeman_id) if user else 'Клиент не найден'
        entry=f'🧾 #{pay.id} · {money(pay.amount)}\n\n{operation(pay.status)} · {NAMES.get(pay.status,pay.status)}\n\n👤 {label} · {user.display_name if user else "—"}\n\n🕒 {pay.created_at:%d.%m.%Y %H:%M} UTC'
        cells.append([cell(f'#{pay.id}\n{pay.created_at:%d.%m.%Y %H:%M}'),cell(f'{label}\n{user.display_name if user else "—"}'),cell(money(pay.amount),align='right'),cell(f'{operation(pay.status)}\n{NAMES.get(pay.status,pay.status)}')])
        text+='\n\n'+entry
    if data:blocks.append(dict(type='table',cells=cells,is_bordered=True,is_striped=True,is_compact=True,caption=f'Операции {offset+1}–{offset+min(5,len(data))} из {count}'))
    if not data:blocks.append(dict(type='paragraph',text='За этот период платежей нет.'))
    rows=[[('24 часа','admin:topups:0:1'),('7 дней','admin:topups:0:7'),('30 дней','admin:topups:0:30')],[('90 дней','admin:topups:0:90'),('Всё время','admin:topups:0:0')]]
    nav=[]
    if offset:nav.append(('← Предыдущие',f'admin:topups:{max(0,offset-5)}:{days}'))
    if len(data)>5:nav.append(('Следующие →',f'admin:topups:{offset+5}:{days}'))
    if nav:rows.append(nav)
    rows += [[('📄 CSV',back+':csv'),('📗 Excel',back+':xlsx')],[('🔄 Обновить',back)],[('← Назад','admin:commerce')]]
    return Screen(escape(text),rows,rich=InputRichMessage(blocks=blocks,skip_entity_detection=True),structured=True)
