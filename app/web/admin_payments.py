from app.web.request_performance import measured_route
from app.web.access import require_business_admin
from app.services.customer_identity import display_name
from types import SimpleNamespace
"""Read-only operational payment registry. PostgreSQL pagination; integer kopecks.
Captured external RUB funds never include wallet debits or Stars RUB estimates.
Only immutable checkout/sale metadata is used, never today's tariff price.
"""
import csv
import io
import re
from decimal import Decimal, InvalidOperation
from datetime import timedelta
from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from app.web.admin_reports import TZ, period_bounds, previous_start
from app.database.models import now

# Settings below are private checkout records. Whitelist fields in SQL; never emit
# their raw JSON, return URLs, invoice payloads, tokens or idempotency keys.
LEDGER = r"""
WITH raw AS (
 SELECT p.*,coalesce(s.value::jsonb,'{}') snap,
 coalesce(o.value::jsonb,st.value::jsonb,pr.value::jsonb,'{}') ord,
 (o.key IS NOT NULL OR st.key IS NOT NULL OR pr.key IS NOT NULL) has_order,
 EXISTS(SELECT 1 FROM balance_transactions b WHERE b.type='yookassa_topup'
 AND b.description=('Пополнение ЮKassa #'||p.id||' · ТЕСТ')) test_topup
 FROM payments p LEFT JOIN app_settings s ON s.key='sale_snapshot:'||p.id
 LEFT JOIN app_settings o ON o.key='order:'||p.id
 LEFT JOIN app_settings st ON st.key='stars_order:'||p.id
 LEFT JOIN app_settings pr ON pr.key='proxy_order:'||p.id
), normalized AS (
 SELECT 'P'||p.id id,p.id number,'payment' record_kind,p.user_id,p.subscription_id,
 coalesce((snap->>'paid_at')::timestamptz,(snap->>'issued_at')::timestamptz,p.created_at) dt,p.created_at,
 CASE WHEN status='balance_paid' THEN -p.amount ELSE p.amount END::bigint amount,
 p.status raw_status,
 CASE WHEN status IN ('succeeded','topup_credited','order_paid','balance_paid','stars_paid','proxy_order_paid','manual_paid') THEN 'success'
 WHEN status='refunded' THEN 'refund'
 WHEN status LIKE '%canceled' OR status IN ('cancelled','canceled') THEN 'cancelled'
 WHEN status IN ('failed','error') OR status LIKE '%failed' THEN 'error'
 WHEN status IN ('manual_preparing','manual_received_pending') OR status LIKE '%processing' THEN 'processing'
 WHEN status='manual_unpaid' THEN 'pending'
 WHEN status='pending' OR status LIKE '%pending' OR status LIKE '%created' THEN 'pending' ELSE 'unknown' END status,
 CASE WHEN status LIKE 'manual_%' AND p.amount>0 THEN 'manual_sale' WHEN status LIKE 'manual_%' THEN 'manual_issue' WHEN status LIKE 'topup_%' OR status='succeeded' THEN 'topup'
 WHEN snap->>'kind'='proxy' OR status LIKE 'proxy_%' THEN 'proxy'
 WHEN coalesce(snap->>'renewal','false')='true' OR coalesce(ord->>'sid','0') NOT IN ('0','') THEN 'renewal'
 WHEN subscription_id IS NOT NULL OR status LIKE 'order_%' OR status LIKE 'stars_%' THEN 'purchase' ELSE 'payment' END operation,
 CASE WHEN status='balance_paid' OR ord->>'method'='balance' THEN 'Баланс' WHEN status LIKE 'stars_%' THEN 'Telegram Stars' WHEN status LIKE 'manual_%' THEN 'Вручную' ELSE '' END method,
 CASE WHEN status='balance_paid' OR ord->>'method'='balance' THEN 'Внутренний баланс' WHEN status LIKE 'stars_%' THEN 'Telegram'
 WHEN status LIKE 'topup_%' OR status LIKE 'order_%' OR status LIKE 'proxy_order_%' THEN 'ЮKassa' WHEN status LIKE 'manual_%' THEN 'Вручную' ELSE '' END provider,
 provider_id,CASE WHEN has_order THEN 'ORD-'||p.id ELSE '' END order_id,
 coalesce((snap->>'months')::int,(ord->>'months')::int,CASE WHEN ord ? 'days' THEN 1 END) months,
 coalesce((snap->>'devices')::int,(ord->>'device_limit')::int,(ord->>'limit')::int) devices,
 (ord->>'stars')::int stars,coalesce((ord->>'balance_applied')::bigint,0) balance_applied,
 CASE WHEN ord->>'method'='balance' THEN 0 WHEN status IN ('succeeded','topup_credited','order_paid','proxy_order_paid','manual_paid') THEN p.amount ELSE 0 END::bigint external,
 coalesce(snap->>'tariff_name','')::text description,NOT(snap ? 'paid_at') date_is_created
 FROM raw p WHERE coalesce(snap->>'test',ord->>'test','false')!='true'
 AND coalesce(snap->>'deleted','false')!='true' AND NOT test_topup AND coalesce(provider_id,'') NOT LIKE 'gift:%'
 UNION ALL
 SELECT 'B'||b.id,b.id,'balance',b.user_id,NULL,b.created_at,b.created_at,b.amount::bigint,
 b.type,'success',b.type,'Баланс','Внутренний баланс',NULL,'',NULL,NULL,NULL,0,0,b.description,true
 FROM balance_transactions b
 WHERE b.type NOT IN ('proxy_balance_v2','manual_sale_income') AND NOT (b.type='yookassa_topup' AND EXISTS(SELECT 1 FROM payments p WHERE b.description='Пополнение ЮKassa #'||p.id))
 AND NOT (b.type IN ('subscription_purchase','auto_renew') AND EXISTS(SELECT 1 FROM payments p WHERE p.status='balance_paid' AND p.user_id=b.user_id AND p.amount=-b.amount AND b.description LIKE '%#'||p.subscription_id||' · %' AND abs(extract(epoch from (p.created_at-b.created_at)))<5))
 AND b.description NOT ILIKE '% · ТЕСТ%'
), ledger AS (
 SELECT n.*,coalesce(u.first_name,'') owner,u.freeman_id,u.username,u.email,u.telegram_id,
 CASE WHEN n.raw_status LIKE 'manual_%' THEN 'manual' ELSE coalesce(u.registration_source,'unknown') END source,
 coalesce(c.access_key_id,CASE WHEN sk.value ~ '^\d+$' THEN sk.value::int END) key_id
 FROM normalized n JOIN users u ON u.id=n.user_id
 LEFT JOIN vless_credentials c ON c.subscription_id=n.subscription_id
 LEFT JOIN app_settings sk ON sk.key='subscription_key:'||n.subscription_id
), filtered AS (
 SELECT * FROM ledger WHERE (:status='all' OR status=:status)
 AND (:operation='all' OR operation=:operation OR (:operation='cash' AND record_kind='payment' AND operation NOT IN ('manual_issue','manual_sale') AND amount!=0) OR (:operation='manual' AND operation IN ('manual_issue','manual_sale')) OR (:operation='wallet' AND record_kind='balance')) AND (:source='all' OR source=:source)
 AND (:method='all' OR method=:method) AND (:provider='all' OR provider=:provider)
 AND (:client='' OR concat_ws(' ',owner,freeman_id,username,email,telegram_id::text,user_id::text) ILIKE :client_like ESCAPE '\')
 AND (CAST(:min_amount AS bigint) IS NULL OR amount>=CAST(:min_amount AS bigint)) AND (CAST(:max_amount AS bigint) IS NULL OR amount<=CAST(:max_amount AS bigint))
 AND (:plan='all' OR coalesce(months::text,'?')||':'||coalesce(devices::text,'?')=:plan)
 AND (:q='' OR concat_ws(' ',owner,freeman_id,username,email,telegram_id::text,user_id::text,
 id,number::text,'PAY-'||number,'#'||number,provider_id,order_id,subscription_id::text,key_id::text,
 operation,method,provider,source,description,
 CASE operation WHEN 'topup' THEN 'Пополнение баланса' WHEN 'purchase' THEN 'Покупка подписки'
 WHEN 'manual_issue' THEN 'Выдача администратора' WHEN 'manual_sale' THEN 'Ручная продажа' WHEN 'renewal' THEN 'Продление подписки' WHEN 'proxy' THEN 'Telegram Proxy'
 WHEN 'proxy_purchase' THEN 'Покупка Telegram Proxy' ELSE operation END,
 (amount::numeric/100)::text,months::text||' мес.',devices::text||' устр.') ILIKE :q_like ESCAPE '\')
), selected AS (SELECT * FROM filtered WHERE dt>=:lo AND dt<:hi)
"""
STATUS = {'success':'Успешно','pending':'Ожидает оплаты','processing':'В обработке','error':'Ошибка','cancelled':'Отменён','refund':'Возврат','unknown':'Неизвестен'}
OPERATIONS = {'manual_issue':'Выдача администратора','manual_sale':'Ручная продажа','purchase':'Покупка подписки','renewal':'Продление подписки','proxy':'Покупка Telegram Proxy','topup':'Пополнение баланса','payment':'Платёж','proxy_purchase':'Покупка Telegram Proxy','admin_credit':'Ручное зачисление','admin_debit':'Ручное списание','referral_reward':'Реферальное начисление','referral_reversal':'Отмена реферального начисления','checkout_hold':'Резерв баланса','checkout_release':'Возврат резерва','gift_purchase':'Покупка подарка','gift_refund':'Возврат подарка','subscription_purchase':'Покупка подписки с баланса','auto_renew':'Автопродление'}
SORTS = {'date_desc':'dt DESC,id DESC','date_asc':'dt ASC,id ASC','amount_desc':'amount DESC,dt DESC,id DESC','amount_asc':'amount ASC,dt DESC,id DESC','status':'status ASC,dt DESC,id DESC'}
def amount_filter(value):
    if not value:return None
    try:
        d=Decimal(value.replace(',','.'))*100
        if not d.is_finite() or d!=d.to_integral_value() or abs(d)>10**14:raise ValueError()
        return int(d)
    except (ValueError,InvalidOperation):raise HTTPException(422,'Сумма должна содержать не более двух знаков после запятой')
def like(value):return '%'+value.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
def parameters(lo,hi,**f):
    q=f.get('q','').strip().lstrip('@');client=f.get('client','').strip().lstrip('@')
    if len(q)>200 or len(client)>200:raise HTTPException(422,'Слишком длинный запрос')
    p=dict(lo=lo,hi=hi,q=q,q_like=like(q),client=client,client_like=like(client))
    for k in ('status','operation','source','method','provider','plan'):
        p[k]=f.get(k,'all') or 'all'
        if len(p[k])>100:raise HTTPException(422,'Недопустимый фильтр')
    if p['status']!='all' and p['status'] not in STATUS:raise HTTPException(422,'Неизвестный статус')
    p['min_amount']=amount_filter(f.get('min_amount',''));p['max_amount']=amount_filter(f.get('max_amount',''))
    if p['min_amount'] is not None and p['max_amount'] is not None and p['min_amount']>p['max_amount']:raise HTTPException(422,'Минимальная сумма больше максимальной')
    return p
async def query(session,sql,p):
    rows=[dict(r) for r in (await session.execute(text(LEDGER+sql),p)).mappings()]
    for r in rows:
        if 'owner' in r:r['owner']=display_name(SimpleNamespace(first_name=r['owner'],freeman_id=r.get('freeman_id'),id=r.get('user_id')))
    return rows
def present(r):
    r=dict(r);r['owner']=display_name(SimpleNamespace(first_name=r.get('owner'),freeman_id=r.get('freeman_id'),id=r.get('user_id')));r['date']=r.pop('dt').astimezone(TZ).isoformat();r['created_at']=r['created_at'].astimezone(TZ).isoformat()
    r['status_label']={'manual_unpaid':'Не оплачено · ключ выдан','manual_preparing':'Выдача не подтверждена','manual_received_pending':'Оплата получена · выдача проверяется'}.get(r['raw_status'],STATUS[r['status']]);r['operation_label']=OPERATIONS.get(r['operation'],r['operation'])
    r['tariff']=('Telegram Proxy · ' if r['operation'] in ('proxy','proxy_purchase') else '')+('24 часа' if r['months']==0 else str(r['months'])+' мес.' if r['months'] is not None else '')+(' · '+str(r['devices'])+' устр.' if r['devices'] else '')
    if r['description'] and r['raw_status'].startswith('manual_'):r['tariff']=r['description']+(' · '+str(r['devices'])+' устр.' if r['devices'] else '')
    r['currency']='XTR' if r['raw_status'].startswith('stars_') else 'RUB'
    r['can_confirm_received']=r['raw_status']=='manual_unpaid';r['can_refund']=False;r['commission']=None;r['net']=None;r['refund_amount']=None
    r['pending_long']=r['status'] in ('pending','processing') and __import__('datetime').datetime.fromisoformat(r['date'])<__import__('datetime').datetime.now(TZ)-timedelta(hours=24)
    return r
async def build_payments(session,p,page=1,limit=10,sort='date_desc'):
    count=(await query(session,'SELECT count(*) total FROM selected',p))[0]['total'];page=min(page,max(1,(count+limit-1)//limit))
    items=await query(session,'SELECT * FROM selected ORDER BY '+SORTS[sort]+' LIMIT :limit OFFSET :offset',p|dict(limit=limit,offset=(page-1)*limit))
    # Facets and KPIs have a stable, explicit scope: selected period, no search/filter.
    sql="""SELECT coalesce(sum(external),0) turnover,count(*) FILTER(WHERE status='success' AND record_kind='payment' AND amount!=0) successful,
    count(*) FILTER(WHERE status='refund') refunds,count(*) FILTER(WHERE status IN ('error','processing','pending') AND operation!='manual_issue') issues,
    count(*) FILTER(WHERE record_kind='payment' AND operation!='manual_issue' AND amount!=0) total,count(*) FILTER(WHERE status='success' AND record_kind='payment' AND amount!=0) success_count,count(*) FILTER(WHERE status='pending' AND operation!='manual_issue') pending,count(*) FILTER(WHERE status='error' AND operation!='manual_issue') errors,
    count(*) FILTER(WHERE operation='manual_issue') manual_issues,count(*) FILTER(WHERE operation='manual_sale') manual_sales,count(*) FILTER(WHERE record_kind='balance') wallet,count(*) FILTER(WHERE operation='topup') topups FROM ledger WHERE dt>=:a AND dt<:b"""
    stats=(await query(session,sql,p|dict(a=p['lo'],b=p['hi'])))[0];prev_lo=previous_start(p['lo'],p['hi'])
    prior=(await query(session,sql,p|dict(a=prev_lo,b=p['lo'])))[0]
    for k in ('turnover','successful','refunds','issues'):stats[k+'_growth']=float((Decimal(stats[k]-prior[k])*100/Decimal(prior[k])).quantize(Decimal('.1'))) if prior[k] else None
    online_sql="""SELECT coalesce(sum(external) FILTER(WHERE operation NOT IN ('manual_issue','manual_sale')),0) online_turnover,
    coalesce(sum(external) FILTER(WHERE operation IN ('manual_issue','manual_sale')),0) manual_revenue,
    count(*) FILTER(WHERE operation NOT IN ('manual_issue','manual_sale') AND status='success' AND record_kind='payment' AND amount!=0) online_successful,
    count(*) FILTER(WHERE operation NOT IN ('manual_issue','manual_sale') AND record_kind='payment' AND amount!=0) online_total,
    count(*) FILTER(WHERE operation NOT IN ('manual_issue','manual_sale') AND status='pending') online_pending,
    count(*) FILTER(WHERE operation NOT IN ('manual_issue','manual_sale') AND status='error') online_errors,
    count(*) FILTER(WHERE operation NOT IN ('manual_issue','manual_sale') AND status='refund') online_refunds
    FROM ledger WHERE dt>=:a AND dt<:b"""
    stats.update((await query(session,online_sql,p|dict(a=p['lo'],b=p['hi'])))[0])
    prior.update((await query(session,online_sql,p|dict(a=prev_lo,b=p['lo'])))[0])
    for k in ('online_turnover','online_successful'):
        stats[k+'_growth']=float((Decimal(stats[k]-prior[k])*100/Decimal(prior[k])).quantize(Decimal('.1'))) if prior[k] else None
    days=(p['hi'].date()-p['lo'].date()).days
    bucket='day' if days<=62 else 'week' if days<=370 else 'month'
    rows=await query(session,"""SELECT to_char(date_trunc(:bucket,dt AT TIME ZONE 'Europe/Moscow'),'YYYY-MM-DD') date,
    coalesce(sum(external) FILTER(WHERE operation NOT IN ('manual_issue','manual_sale')),0) online,
    coalesce(sum(external) FILTER(WHERE operation IN ('manual_issue','manual_sale')),0) manual,
    count(*) FILTER(WHERE status='success' AND operation NOT IN ('manual_issue','manual_sale') AND record_kind='payment' AND amount!=0) successful,
    count(*) FILTER(WHERE status='pending' AND operation NOT IN ('manual_issue','manual_sale') AND record_kind='payment') pending,
    count(*) FILTER(WHERE status='refund' AND operation NOT IN ('manual_issue','manual_sale')) refunds
    FROM ledger WHERE dt>=:lo AND dt<:hi GROUP BY 1 ORDER BY 1""",p|dict(bucket=bucket))
    points={r['date']:r for r in rows};series=[];cursor=p['lo'].astimezone(TZ).replace(hour=0,minute=0,second=0,microsecond=0)
    if bucket=='week':cursor-=timedelta(days=cursor.weekday())
    elif bucket=='month':cursor=cursor.replace(day=1)
    while cursor<p['hi']:
        day=cursor.date().isoformat();row=points.get(day,dict(date=day,online=0,manual=0,successful=0,pending=0,refunds=0))
        row['future']=cursor.date()>now().astimezone(TZ).date();series.append(row)
        cursor=(cursor.replace(year=cursor.year+1,month=1) if cursor.month==12 else cursor.replace(month=cursor.month+1)) if bucket=='month' else cursor+timedelta(days=7 if bucket=='week' else 1)
    choices=await query(session,'SELECT DISTINCT status,operation,source,method,provider,months,devices FROM ledger WHERE dt>=:lo AND dt<:hi',p)
    return dict(series=series,bucket=bucket,items=[present(r) for r in items],total=count,page=page,limit=limit,stats=stats,previous=prior,
        choices={k:sorted({str(r[k]) for r in choices if r[k]}) for k in ('status','operation','source','method','provider')},
        plans=sorted({str(r['months'])+':'+str(r['devices']) for r in choices if r['months'] is not None and r['devices'] is not None}),
        start=p['lo'].date().isoformat(),end=(p['hi']-timedelta(days=1)).date().isoformat(),timezone='Europe/Moscow',
        previous_start=prev_lo.date().isoformat(),previous_end=(p['lo']-timedelta(days=1)).date().isoformat(),
        notes=['Онлайн-поступления и ручные продажи показаны отдельно. Списания баланса и рублёвая оценка Stars не прибавляются повторно.','Показатели и счётчики вкладок относятся ко всему периоду, без поиска и фильтров.','Если время оплаты не сохранено, используется дата создания. Возвраты относятся к дате исходного платежа: дата и сумма возврата отдельно не сохранены.'])

def admin_payments_router(database,settings,current_user):
    router=APIRouter(route_class=measured_route(database.engine))
    async def authorized(token):
        actor=await current_user(token)
        require_business_admin(actor,settings)
    @router.get('/api/web/admin/payments')
    async def payments(period:str='month',start:str='',end:str='',page:int=1,limit:int=10,sort:str='date_desc',q:str='',status:str='all',operation:str='cash',source:str='all',method:str='all',provider:str='all',client:str='',min_amount:str='',max_amount:str='',plan:str='all',export:str='',freeman_session:str|None=Cookie(default=None)):
        await authorized(freeman_session)
        if page<1 or limit not in (10,25,50,100) or sort not in SORTS or export not in ('','csv'):raise HTTPException(422,'Недопустимые параметры')
        lo,hi=period_bounds(period,start,end);p=parameters(lo,hi,q=q,status=status,operation=operation,source=source,method=method,provider=provider,client=client,min_amount=min_amount,max_amount=max_amount,plan=plan)
        if export=='csv':
            async def stream():
                def line(values):
                    out=io.StringIO();csv.writer(out,delimiter=';').writerow(values);return out.getvalue()
                yield '\ufeff'+line(['Операция ID','Дата МСК','Freeman ID','Клиент','Операция','Тариф на момент покупки','Сумма ₽','Количество Stars','Цена тарифа ₽ (Stars)','Способ оплаты','Провайдер','Статус','Payment ID','Заказ','Подписка','Источник','Дата создания использована'])
                async with database.sessions() as session:
                    # One consistent snapshot across every exported page.
                    await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                    offset=0
                    while True:
                        rows=await query(session,'SELECT * FROM selected ORDER BY '+SORTS[sort]+' LIMIT 500 OFFSET :offset',p|dict(offset=offset))
                        if not rows:break
                        def safe(v):
                            s=str(v or '');return "'"+s if s.startswith(('=','+','-','@','\t','\r')) else s
                        for raw in rows:
                            r=present(raw)
                            yield line([r['id'],r['date'],safe(r['freeman_id']),safe(r['owner']),safe(r['operation_label']),safe(r['tariff']),str(Decimal(r['amount'])/100) if r['currency']=='RUB' else '',r['stars'] or '',str(Decimal(r['amount'])/100) if r['currency']=='XTR' else '',safe(r['method']),safe(r['provider']),r['status_label'],safe(r['provider_id']),safe(r['order_id']),r['subscription_id'] or '',safe(r['source']),r['date_is_created']])
                        offset+=500
            return StreamingResponse(stream(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="payments-{lo:%Y%m%d}-{hi-timedelta(days=1):%Y%m%d}.csv"','Cache-Control':'no-store'})
        async with database.sessions() as session:
            await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            return await build_payments(session,p,page,limit,sort)
    @router.get('/api/web/admin/payments/{record_id}')
    async def payment(record_id:str,freeman_session:str|None=Cookie(default=None)):
        await authorized(freeman_session)
        if not re.fullmatch(r'[PB][1-9]\d{0,10}',record_id):raise HTTPException(404,'Операция не найдена')
        async with database.sessions() as session:
            # Detail is independent from the currently selected list/page filters.
            rows=[dict(r) for r in (await session.execute(text(LEDGER.split(', filtered AS')[0]+' SELECT * FROM ledger WHERE id=:id'),dict(id=record_id))).mappings()]
        if not rows:raise HTTPException(404,'Операция не найдена')
        result=present(rows[0])
        actor=await current_user(freeman_session)
        result["can_correct_price"]=False
        if record_id.startswith("P") and settings.is_owner(actor.telegram_id) and result.get("raw_status") in ("manual_paid","manual_unpaid"):
            result["can_correct_price"]=True
        return result
    return router
