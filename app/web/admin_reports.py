from app.web.request_performance import measured_route
from app.web.access import require_business_admin
from app.services.customer_identity import display_name
from types import SimpleNamespace
"""Central reporting definitions. Money is integer kopecks, dates are Moscow.
Only settled product orders are sales; funding/free grants are not revenue.
All summaries are aggregated in PostgreSQL; only a paginated ledger is returned.
"""
import json,csv,io
from datetime import datetime,timedelta,timezone
from decimal import Decimal,ROUND_HALF_UP
from zoneinfo import ZoneInfo
from fastapi import APIRouter,Cookie,HTTPException
from fastapi.responses import Response
from sqlalchemy import text
from app.database.models import now
TZ=ZoneInfo('Europe/Moscow')
PAID={'order_paid','balance_paid','stars_paid','proxy_order_paid','manual_paid'}
def aware(d):return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
async def record_sale(repo,payment,months=0,devices=0,total=None,kind='vpn',renewal=False,tariff=None,source=None):
    key=f'sale_snapshot:{payment.id}'
    if await repo.setting(key):return
    snapshot=dict(months=months,devices=devices,total=payment.amount if total is None else total,kind=kind,renewal=renewal,paid_at=now().isoformat(),source=source or await repo.setting(f'sale_source:{payment.id}','unknown'))
    if tariff:snapshot.update(tariff_id=tariff.id,tariff_name=await repo.tariff_setting(tariff,'tariff_name',f'{months} мес.'))
    await repo.set_setting(key,json.dumps(snapshot))
def period_bounds(period,start,end):
    today=now().astimezone(TZ).date()
    if period=='custom':
        try:a=datetime.fromisoformat(start).date();b=datetime.fromisoformat(end).date()
        except (ValueError,TypeError):raise HTTPException(422,'Укажите начало и конец периода')
    elif period=='day':a=b=today
    elif period=='week':a=today-timedelta(days=today.weekday());b=a+timedelta(days=6)
    elif period=='month':a=today.replace(day=1);b=(a.replace(year=a.year+1,month=1) if a.month==12 else a.replace(month=a.month+1))-timedelta(days=1)
    elif period=='year':a=today.replace(month=1,day=1);b=today.replace(month=12,day=31)
    else:raise HTTPException(422,'Неизвестный период')
    if a>b or (b-a).days>3660:raise HTTPException(422,'Период должен быть от 1 дня до 10 лет')
    return datetime.combine(a,datetime.min.time(),TZ),datetime.combine(b+timedelta(days=1),datetime.min.time(),TZ)
def previous_start(lo,hi):
    if lo.day==hi.day==1 and lo.month==hi.month==1 and hi.year==lo.year+1:return lo.replace(year=lo.year-1)
    if lo.day==hi.day==1 and hi.year*12+hi.month-lo.year*12-lo.month==1:return lo.replace(year=lo.year-1,month=12) if lo.month==1 else lo.replace(month=lo.month-1)
    return lo-(hi-lo)
def rounded(n,d):return int((Decimal(n)/Decimal(d)).quantize(Decimal('1'),rounding=ROUND_HALF_UP)) if d else 0
# Immutable snapshots keep checkout prices/plan parameters; tariff prices never recalculate old sales.
SALES="""
WITH raw AS (
 SELECT p.*,coalesce(s.value::jsonb,'{}'::jsonb) snap,coalesce(o.value::jsonb,'{}'::jsonb) ord
 FROM payments p LEFT JOIN app_settings s ON s.key='sale_snapshot:'||p.id
 LEFT JOIN app_settings o ON o.key=(CASE p.status WHEN 'order_paid' THEN 'order:' WHEN 'stars_paid' THEN 'stars_order:' ELSE 'proxy_order:' END)||p.id
 WHERE p.status IN ('order_paid','balance_paid','stars_paid','proxy_order_paid','manual_paid') AND p.amount>=0
), normalized AS (
 SELECT p.id::text id,p.user_id,p.subscription_id,
 coalesce((snap->>'paid_at')::timestamptz,p.created_at) dt,
 coalesce(snap->>'kind',CASE WHEN status='proxy_order_paid' THEN 'proxy' ELSE 'vpn' END) kind,
 coalesce((snap->>'total')::bigint,(ord->>'total')::bigint,p.amount)::bigint amount,
 coalesce((snap->>'months')::int,(ord->>'months')::int) months,
 coalesce((snap->>'devices')::int,(ord->>'device_limit')::int,(ord->>'limit')::int) devices,
 CASE WHEN snap ? 'renewal' THEN (snap->>'renewal')::boolean WHEN ord ? 'sid' THEN (ord->>'sid') IS NOT NULL ELSE NULL END renewal,
 CASE WHEN ord->>'method'='balance' THEN 'Баланс' ELSE CASE status WHEN 'balance_paid' THEN 'Баланс' WHEN 'stars_paid' THEN 'Telegram Stars' WHEN 'manual_paid' THEN 'Вручную' ELSE CASE WHEN (ord->>'balance_applied')::bigint>0 THEN 'Карта + баланс' ELSE 'Карта' END END END channel,
 CASE WHEN ord->>'method'='balance' THEN 0 WHEN status IN ('order_paid','proxy_order_paid','manual_paid') THEN p.amount ELSE 0 END external,
 NOT (snap ? 'paid_at') historical, (snap->>'tariff_id')::int tariff_id, snap->>'tariff_name' tariff_name, coalesce(snap->>'source','unknown') sales_source
 FROM raw p WHERE coalesce(snap->>'test',ord->>'test','false')!='true' AND coalesce(snap->>'deleted','false')!='true'
 UNION ALL
 SELECT 'B'||id,user_id,NULL,created_at,'proxy',-amount,1,NULL,NULL,'Баланс',0,false,NULL,NULL,'unknown'
 FROM balance_transactions WHERE type='proxy_purchase' AND amount<0
), all_sales AS (
 SELECT n.*,coalesce(u.first_name,'') owner,u.freeman_id,
 CASE WHEN u.referrer_id IS NOT NULL THEN 'referral' ELSE coalesce(u.registration_source,'unknown') END source
 FROM normalized n JOIN users u ON u.id=n.user_id WHERE u.status!='deleted' AND n.amount>0
), filtered AS (
 SELECT a.* FROM all_sales a
 WHERE (:kind='all' OR kind=:kind) AND (:source='all' OR source=:source)
 AND (:months=-1 OR months=:months) AND (:devices=0 OR devices=:devices)
 AND (:channel='all' OR channel=:channel) AND (:sale_type='all' OR (:sale_type='renewal' AND renewal=true) OR (:sale_type='new' AND renewal=false))
), selected AS (SELECT * FROM filtered WHERE dt>=:lo AND dt<:hi)
"""
async def build_report(session,lo,hi,kind='all',page=1,limit=25,source='all',months=-1,devices=0,channel='all',sale_type='all'):
    if kind not in ('all','vpn','proxy') or sale_type not in ('all','new','renewal'):raise HTTPException(422,'Неизвестный фильтр')
    prior_lo=previous_start(lo,hi);days=(hi.date()-lo.date()).days;bucket='month' if days>120 else 'week' if days>45 else 'day'
    params=dict(lo=lo,hi=hi,prior_lo=prior_lo,kind=kind,source=source,months=months,devices=devices,channel=channel,sale_type=sale_type,bucket=bucket)
    from app.web.report_snapshot import sales_snapshot, SUMMARY, SELECTS
    snapshot=await sales_snapshot(session,SALES,params|dict(limit=limit,offset=(page-1)*limit))
    async def query(sql,extra=None,cte=True):
        if cte:
            key=('q0' if extra['a']==lo else 'q1') if sql==SUMMARY else 'q'+str(SELECTS.index(sql)+2)
            rows=[dict(r) for r in snapshot[key]]
        else:rows=[dict(r) for r in (await session.execute(text(sql),params|(extra or {}))).mappings()]
        for r in rows:
            if 'owner' in r:r['owner']=display_name(SimpleNamespace(first_name=r['owner'],freeman_id=r.get('freeman_id'),id=r.get('user_id')))
        return rows
    summary="""SELECT count(*) sales,coalesce(sum(amount),0) revenue,count(DISTINCT user_id) buyers,count(*) FILTER(WHERE renewal=true) renewals,coalesce(sum(amount) FILTER(WHERE renewal=true),0) renewal_revenue,count(*) FILTER(WHERE historical) historical FROM filtered WHERE dt>=:a AND dt<:b"""
    stats=(await query(summary,dict(a=lo,b=hi)))[0];prior=(await query(summary,dict(a=prior_lo,b=lo)))[0]
    for row in (stats,prior):row['average']=rounded(row['revenue'],row['sales'])
    # Registrations use unified customer IDs and a verifiable acquisition source.
    regsql="""SELECT count(*) count FROM users WHERE status!='deleted' AND created_at>=:a AND created_at<:b AND (:source='all' OR CASE WHEN referrer_id IS NOT NULL THEN 'referral' ELSE coalesce(registration_source,'unknown') END=:source)"""
    stats['new_customers']=(await query(regsql,dict(a=lo,b=hi),False))[0]['count'];prior['new_customers']=(await query(regsql,dict(a=prior_lo,b=lo),False))[0]['count']
    for key in ('revenue','sales','buyers','average','renewals','new_customers'):
        stats[key+'_growth']=float((Decimal(stats[key]-prior[key])*100/Decimal(prior[key])).quantize(Decimal('.1'))) if prior[key] else None
    series=await query("""SELECT to_char(date_trunc(:bucket,dt AT TIME ZONE 'Europe/Moscow'),'YYYY-MM-DD') date,count(*) sales,sum(amount) revenue,count(*) FILTER(WHERE kind='vpn') vpn,count(*) FILTER(WHERE kind='proxy') proxy,count(*) FILTER(WHERE renewal=true) renewals,count(*) FILTER(WHERE renewal=false) new_sales,coalesce(sum(amount) FILTER(WHERE renewal=true),0) renewal_revenue FROM selected GROUP BY 1 ORDER BY 1""")
    # Zero buckets are real absence of activity, not loading/demo values.
    by={r['date']:r for r in series};cursor=lo.replace(hour=0,minute=0,second=0,microsecond=0);filled=[]
    if bucket=='week':cursor=lo-timedelta(days=lo.weekday())
    if bucket=='month':cursor=lo.replace(day=1)
    while cursor<hi:
        k=cursor.strftime('%Y-%m-%d');point=by.get(k,dict(date=k,sales=0,revenue=0,vpn=0,proxy=0,renewals=0,new_sales=0,renewal_revenue=0));point['future']=cursor.date()>now().astimezone(TZ).date();filled.append(point)
        cursor=(cursor.replace(year=cursor.year+1,month=1) if cursor.month==12 else cursor.replace(month=cursor.month+1)) if bucket=='month' else cursor+timedelta(days=7 if bucket=='week' else 1)
    tariffs=await query("""SELECT kind,months,devices,tariff_id,
 (array_agg(tariff_name ORDER BY dt DESC,id DESC) FILTER(WHERE tariff_name IS NOT NULL))[1] tariff_name,
 count(*) sales,sum(amount) revenue,count(DISTINCT user_id) buyers,min(amount) min_price,max(amount) max_price
 FROM selected GROUP BY kind,months,devices,tariff_id,CASE WHEN tariff_id IS NULL THEN tariff_name END ORDER BY revenue DESC""")
    def title(r):
        if r.get('tariff_name'):return r['tariff_name']+(' · '+str(r['devices'])+' устр.' if r.get('devices') else '')
        if r['kind']=='proxy':return 'Telegram Proxy'+(' · '+str(r['months'])+' мес.' if r['months'] else '')
        m=r['months'];d=r.get('devices');return ('24 часа' if m==0 else str(m)+' мес.' if m else 'Срок не сохранён')+(' · '+str(d)+' устр.' if d else ' · лимит не сохранён')
    for t in tariffs:t.update(name=title(t),share=round(t['sales']/max(1,stats['sales'])*100,1))
    sales_channels=await query('SELECT sales_source source,count(*) sales,coalesce(sum(amount),0) revenue FROM selected GROUP BY sales_source')
    # Historical bot grants have a recognition date, not a reconstructed
    # issuance date. Include them without inventing an original issue time.
    manual=(await query("""WITH manual_records AS (
 SELECT p.*,CASE WHEN starts_with(s.key,'sale_snapshot:') THEN s.value::jsonb END snap
 FROM payments p JOIN app_settings s ON s.key='sale_snapshot:'||p.id
 WHERE p.status IN ('manual_paid','manual_unpaid')
) SELECT count(*) sales,coalesce(sum(amount),0) gross,
 coalesce(sum(amount) FILTER(WHERE status='manual_paid'),0) collected,
 coalesce(sum(amount) FILTER(WHERE status='manual_unpaid'),0) unpaid,
 count(*) FILTER(WHERE status='manual_unpaid') unpaid_sales
 FROM manual_records
 WHERE coalesce(snap->>'test','false')!='true' AND coalesce(snap->>'deleted','false')!='true'
 AND coalesce((snap->>'issued_at')::timestamptz,(snap->>'paid_at')::timestamptz,created_at)>=:lo
 AND coalesce((snap->>'issued_at')::timestamptz,(snap->>'paid_at')::timestamptz,created_at)<:hi""",cte=False))[0]
    issuances=await query("""SELECT (CASE WHEN starts_with(key,'manual_sale:') THEN value::jsonb END)->'snapshot'->>'issuance_kind' category,count(*) count FROM app_settings WHERE key LIKE 'manual_sale:%' AND (CASE WHEN starts_with(key,'manual_sale:') THEN value::jsonb END)->'result'->>'done'='true' AND ((CASE WHEN starts_with(key,'manual_sale:') THEN value::jsonb END)->'snapshot'->>'issued_at')::timestamptz>=:lo AND ((CASE WHEN starts_with(key,'manual_sale:') THEN value::jsonb END)->'snapshot'->>'issued_at')::timestamptz<:hi GROUP BY 1""",cte=False)
    channels={r['channel']:r['revenue'] for r in await query('SELECT channel,sum(amount) revenue FROM selected GROUP BY channel')}
    sources=await query('SELECT source,count(DISTINCT user_id) count FROM selected GROUP BY source ORDER BY count DESC')
    customers=await query("""SELECT to_char(date_trunc(:bucket,s.dt AT TIME ZONE 'Europe/Moscow'),'YYYY-MM-DD') date,count(DISTINCT s.user_id) FILTER(WHERE f.first_date>=:lo) new_customers,count(DISTINCT s.user_id) FILTER(WHERE f.first_date<:lo) "returning" FROM selected s JOIN (SELECT user_id,min(dt) first_date FROM all_sales GROUP BY user_id) f USING(user_id) GROUP BY 1 ORDER BY 1""")
    breakdown=await query("""SELECT CASE WHEN kind='proxy' THEN 'Telegram Proxy' WHEN renewal=true THEN 'Продления' WHEN renewal=false THEN 'Новые подписки' ELSE 'Тип покупки не сохранён' END name,sum(amount) revenue,count(*) sales FROM selected GROUP BY 1 ORDER BY revenue DESC""")
    # Current operational state is explicitly separate from historical sales.
    # Compile a complete correlated query, never a detached active_key() expression:
    # compiling the predicate alone adds an inner `k` FROM and stops every key
    # as soon as ANY unrelated subscription has been removed.
    operational=await query("""WITH links AS (
 SELECT access_key_id::text key_id,subscription_id sid FROM vless_credentials
 UNION SELECT access_key_id::text,subscription_id FROM vpn_credentials
 UNION SELECT a.value,s.id FROM subscriptions s JOIN app_settings a
 ON a.key='subscription_key:'||s.id
), keys AS (
 SELECT k.*,coalesce(v.device_limit,s.device_limit) device_limit,v.last_sync,
 (SELECT count(*) FROM devices d WHERE d.subscription_id=s.id AND d.status='active') connected,
 CASE
 WHEN k.status IN ('blocked','disabled','billing_blocked') OR v.status IN ('blocked','disabled') THEN 'blocked'
 WHEN k.expires_at<=:now OR k.status='expired' OR v.status='expired' THEN 'expired'
 WHEN EXISTS(SELECT 1 FROM links l JOIN app_settings a ON
 (a.key='subscription_status:'||l.sid AND a.value!='active') OR
 (a.key='subscription_removed:'||l.sid AND a.value='1') OR
 (a.key='subscription_transferred:'||l.sid AND a.value='1') WHERE l.key_id=k.id::text)
 OR k.status IN ('stopped','paused') OR v.status IN ('stopped','paused') THEN 'stopped'
 WHEN k.status!='active' OR (v.id IS NOT NULL AND v.status!='active') OR k.expires_at IS NULL THEN 'unknown'
 WHEN k.expires_at<=:soon THEN 'expiring' ELSE 'active' END state
 FROM access_keys k LEFT JOIN vless_credentials v ON v.access_key_id=k.id
 LEFT JOIN subscriptions s ON s.id=(SELECT min(l.sid) FROM links l WHERE l.key_id=k.id::text)
 WHERE k.status!='deleted'
) SELECT state,count(*) count,coalesce(sum(connected),0) connected,coalesce(sum(device_limit),0) capacity,count(*) FILTER(WHERE connected=0) empty,count(*) FILTER(WHERE connected>=device_limit AND device_limit>0) at_limit,min(last_sync) last_sync FROM keys GROUP BY state""",dict(now=now(),soon=now()+timedelta(days=7)),False)
    statuses={r['state']:r['count'] for r in operational};active=statuses.get('active',0)+statuses.get('expiring',0)
    usage=dict(connected=sum(r['connected'] for r in operational),capacity=sum(r['capacity'] for r in operational),empty=sum(r['empty'] for r in operational),at_limit=sum(r['at_limit'] for r in operational),last_sync=min((r['last_sync'].isoformat() for r in operational if r['last_sync']),default=None))
    proxy=(await query("""SELECT count(*) FILTER(WHERE status='active' AND expires_at>:now) active,count(*) FILTER(WHERE status='active' AND expires_at>:now AND expires_at<=:soon) expiring FROM proxy_credentials WHERE status!='deleted'""",dict(now=now(),soon=now()+timedelta(days=7)),False))[0]
    proxy.update(revenue=sum(r['revenue'] for r in breakdown if r['name']=='Telegram Proxy'),paid_sales=sum(r['sales'] for r in breakdown if r['name']=='Telegram Proxy'))
    payments=await query("""SELECT status,count(*) count,coalesce(sum(amount),0) amount FROM payments WHERE created_at>=:lo AND created_at<:hi GROUP BY status""",cte=False)
    # Refund linkage/date and provider commission are not recorded reliably; do not invent net profit.
    finance=(await query("""SELECT coalesce((SELECT sum(amount) FROM expenses WHERE created_at>=:lo AND created_at<:hi),0) expenses,coalesce((SELECT sum(amount) FROM referral_rewards WHERE created_at>=:lo AND created_at<:hi AND status='credited'),0) referral_rewards""",cte=False))[0]
    finance.update(external_sales=(await query('SELECT coalesce(sum(external),0) value FROM selected'))[0]['value'],wallet_funding=sum(r['amount'] for r in payments if r['status']=='succeeded'),refunds=sum(r['amount'] for r in payments if r['status']=='refunded'),profit=None,refund_attribution=False)
    total=stats['sales'];page=max(1,min(page,max(1,(total+limit-1)//limit)))
    items=await query('SELECT * FROM selected ORDER BY dt DESC,id DESC LIMIT :limit OFFSET :offset',dict(limit=limit,offset=(page-1)*limit))
    for r in items:r.update(date=r.pop('dt').astimezone(TZ).isoformat(),tariff=title(r))
    return dict(start=lo.date().isoformat(),end=(hi-timedelta(days=1)).date().isoformat(),previous_start=prior_lo.date().isoformat(),previous_end=(lo-timedelta(days=1)).date().isoformat(),timezone='Europe/Moscow',stats=stats,previous=prior,manual=manual,issuances=issuances,sales_channels=sales_channels,active_keys=active,expiring=statuses.get('expiring',0),renewals=stats['renewals'],series=filled,bucket=bucket,tariffs=tariffs,channels=channels,sources=sources,customers=customers,breakdown=breakdown,statuses=statuses,devices=usage,proxy=proxy,payments=payments,finance=finance,historical=stats['historical'],items=items,total=total,page=page,limit=limit,generated_at=now().isoformat())
def admin_reports_router(database,settings,current_user):
    from app.web.admin_digest import digest_router
    router=APIRouter(route_class=measured_route(database.engine))
    router.include_router(digest_router(database,settings,current_user))
    @router.get('/api/web/admin/reports')
    async def report(period:str='month',start:str='',end:str='',kind:str='all',page:int=1,limit:int=25,export:str='',source:str='all',months:int=-1,devices:int=0,channel:str='all',sale_type:str='all',freeman_session:str|None=Cookie(default=None)):
        actor=await current_user(freeman_session)
        require_business_admin(actor,settings)
        if limit not in (10,25,50,100) or page<1 or months< -1 or devices<0:raise HTTPException(422,'Недопустимый фильтр')
        lo,hi=period_bounds(period,start,end)
        async with database.sessions() as session:
            result=await build_report(session,lo,hi,kind,page,limit,source,months,devices,channel,sale_type)
            if export=='csv':
                out=io.StringIO();writer=csv.writer(out,delimiter=';');writer.writerow(['Платёж','Дата МСК','Freeman ID','Подписка','Сумма ₽','Способ оплаты','Продление'])
                params=dict(lo=lo,hi=hi,kind=kind,source=source,months=months,devices=devices,channel=channel,sale_type=sale_type)
                offset=0
                while True:
                    chunk=(await session.execute(text(SALES+'SELECT * FROM selected ORDER BY dt DESC,id DESC LIMIT 500 OFFSET :offset'),params|dict(offset=offset))).mappings().all()
                    if not chunk:break
                    for r in chunk:
                        name=('Telegram Proxy' if r['kind']=='proxy' else 'Подписка')+(' · '+str(r['months'])+' мес.' if r['months'] is not None else '')+(' · '+str(r['devices'])+' устр.' if r['devices'] else '')
                        def safe(value):
                            v=str(value or '')
                            return "'"+v if v.startswith(('=','+','-','@')) else v
                        writer.writerow([r['id'],r['dt'].astimezone(TZ).isoformat(),safe(r['freeman_id']),safe(r.get('tariff_name') or name),str(Decimal(r['amount'])/100),safe(r['channel']),r['renewal']])
                    offset+=500
                return Response(('\ufeff'+out.getvalue()).encode(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="sales.csv"'})
        return result
    return router
