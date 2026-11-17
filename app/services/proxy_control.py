"""Shared Proxy operational domain; Telemt is authoritative for live metrics."""
import json,re,hashlib
from datetime import timedelta,datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from sqlalchemy import select,func,or_,case
from app.database.models import *
from app.services.offers import utc
from app.services.proxies import ProxyService

DEFAULTS=dict(auto_included=True,eligible_tariffs=[],standalone_enabled=True,default_ip_limit=2,default_tcp_limit=10,grace_days=0,notify_activation=True,notify_expiry=True,reminder_days=[3,1],online_seconds=360)
async def config(repo):
    try:result=DEFAULTS|json.loads(await repo.setting('proxy_control_settings','{}'))
    except (ValueError,TypeError):result=dict(DEFAULTS)
    configured=result.get('reminder_days')
    result['reminder_days']=[d for d in (3,1) if d in (configured if isinstance(configured,list) else [3,1])]
    return result
async def server_config(repo,sid):
    try:return dict(country='',city='',provider='',notes='',host='',port=443,max_accounts=10000,max_clients=10000,traffic_limit=0)|json.loads(await repo.setting(f'proxy_server_config:{sid}','{}'))
    except ValueError:return {}
def fresh(s):return bool(s.last_sync and utc(s.last_sync)>now()-timedelta(seconds=360) and s.health=='online')
def proxy_active():return (ProxyCredential.status=='active') & (ProxyCredential.expires_at>now())
async def meta(repo,c):
    m=await repo.session.scalar(select(ProxyAccessDetails).where(ProxyAccessDetails.credential_id==c.id))
    if m:return m
    kind=await repo.setting(f'proxy_credential_kind:{c.id}')
    m=await repo.add(ProxyAccessDetails(credential_id=c.id,source={'vpn':'included','paid':'standalone'}.get(kind,'unknown'),ip_limit=int(await repo.setting(f'proxy_credential_ip_limit:{c.id}','2'))))
    if kind=='vpn':
        sub=await repo.session.scalar(select(Subscription).where(Subscription.user_id==c.user_id,Subscription.expires_at>now()).order_by(Subscription.expires_at.desc()).limit(1))
        if sub:m.subscription_id=sub.id
    match=re.search(r'_pay(\d+)$',c.remote_username)
    if match:m.payment_id=int(match[1]) if await repo.session.get(Payment,int(match[1])) else None
    return m
async def select_servers(service,kind):
    candidates=await service.session.scalars(select(ProxyServer).where(ProxyServer.status=='active',ProxyServer.audience.in_(('all',kind))).order_by(ProxyServer.priority,ProxyServer.id))
    result=[]
    for s in candidates:
        if not fresh(s):continue
        opts=await server_config(service.repo,s.id)
        n=await service.session.scalar(select(func.count(ProxyCredential.id)).where(ProxyCredential.server_id==s.id,ProxyCredential.status.not_in(('deleted','migrated','missing'))))
        clients=await service.session.scalar(select(func.count(func.distinct(ProxyCredential.user_id))).where(ProxyCredential.server_id==s.id,proxy_active()))
        if n>=opts['max_accounts'] or clients>=opts['max_clients'] or (opts.get('traffic_limit') and s.total_octets>=opts['traffic_limit']):continue
        result.append((n/max(1,opts['max_accounts']),s.priority,s.id,s))
    return [r[-1] for r in sorted(result)]
async def observe(repo,s,rows):
    names={r.get('username'):r for r in rows};day=now().astimezone(ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d')
    for c in await repo.session.scalars(select(ProxyCredential).where(ProxyCredential.server_id==s.id,ProxyCredential.status.not_in(('deleted','migrated')))):
        row=names.get(c.remote_username)
        if not row:continue
        m=await meta(repo,c)
        if row.get('max_unique_ips') is not None:m.ip_limit=int(row['max_unique_ips'])
        current=max(0,int(row.get('total_octets') or 0));previous=await repo.setting(f'proxy_traffic_counter:{c.id}')
        if previous and previous.isdigit():
            old=int(previous);delta=current-old if current>=old else current
            point=await repo.session.scalar(select(ProxyDaily).where(ProxyDaily.credential_id==c.id,ProxyDaily.day==day))
            if not point:point=await repo.add(ProxyDaily(credential_id=c.id,server_id=s.id,day=day,octets=0))
            point.octets+=delta
        await repo.set_setting(f'proxy_traffic_counter:{c.id}',str(current))
        ips=row.get('active_unique_ips_list')
        if ips is not None:
            known=list(await repo.session.scalars(select(ProxyIPSeen).where(ProxyIPSeen.credential_id==c.id)))
            for x in known:x.active=x.ip in ips
            by={x.ip:x for x in known}
            for ip in ips:
                if ip in by:by[ip].last_seen=now();by[ip].active=True
                else:await repo.add(ProxyIPSeen(credential_id=c.id,ip=str(ip),last_seen=now()))
            await repo.set_setting(f'proxy_ips_supported:{c.id}','1')
    await repo.set_setting(f'proxy_telemetry_started:{s.id}',await repo.setting(f'proxy_telemetry_started:{s.id}',now().isoformat()))
async def active_subscription_ids(repo,uid):
    """Resolve active keys once per owner, preserving VLESS/VPN/legacy priority."""
    from app.services.client_registry import active_key
    keys=list(await repo.session.scalars(select(AccessKey).where(AccessKey.user_id==uid,active_key())))
    if not keys:return set()
    remaining={k.id for k in keys};result=set()
    for cls in (VlessCredential,VpnCredential):
        if not remaining:break
        links=list((await repo.session.execute(select(cls.access_key_id,cls.subscription_id).where(cls.access_key_id.in_(remaining)))).all())
        for kid,sid in links:
            if kid in remaining:result.add(sid);remaining.remove(kid)
    if remaining:
        # Legacy subscription_key bindings are considered only without a typed link.
        subs=list(await repo.session.scalars(select(Subscription.id).where(Subscription.user_id==uid)))
        names={f'subscription_key:{sid}':sid for sid in subs}
        bindings=dict((await repo.session.execute(select(AppSetting.key,AppSetting.value).where(AppSetting.key.in_(names)))).all()) if names else {}
        for sid in subs:
            value=bindings.get(f'subscription_key:{sid}')
            for kid in tuple(remaining):
                if value==str(kid):result.add(sid);remaining.remove(kid)
    return result

async def eligible_subscriptions(repo,uid,candidates,cfg):
    active=await active_subscription_ids(repo,uid)
    candidates=[s for s in candidates if s.id in active and utc(s.expires_at)>now() and (not cfg['eligible_tariffs'] or s.tariff_id in cfg['eligible_tariffs'])]
    names=[f'subscription_{field}:{s.id}' for s in candidates for field in ('status','removed','transferred')]
    flags=dict((await repo.session.execute(select(AppSetting.key,AppSetting.value).where(AppSetting.key.in_(names)))).all()) if names else {}
    return [s for s in candidates if flags.get(f'subscription_status:{s.id}','active')=='active' and all(flags.get(f'subscription_{field}:{s.id}')!='1' for field in ('removed','transferred'))]

async def subscription_eligible(repo,sub):
    return bool(await eligible_subscriptions(repo,sub.user_id,[sub],await config(repo)))
async def sync_included(repo,settings,sub,create=False,*,api_timeout=10):
    service=ProxyService(repo,settings,api_timeout=api_timeout);cfg=await config(repo)
    # Serialise entitlement changes and issuance across all keys of one owner.
    user=await repo.session.scalar(select(User).where(User.id==sub.user_id).with_for_update())
    candidates=list(await repo.session.scalars(select(Subscription).where(Subscription.user_id==sub.user_id).order_by(Subscription.expires_at.desc())))
    eligible=[]
    if user and user.status!='blocked':
        eligible=await eligible_subscriptions(repo,sub.user_id,candidates,cfg)
    rows=list(await repo.session.scalars(select(ProxyCredential).join(ProxyAccessDetails,ProxyAccessDetails.credential_id==ProxyCredential.id).where(ProxyCredential.user_id==sub.user_id,ProxyAccessDetails.source=='included',ProxyCredential.status.not_in(('deleted','migrated'))).order_by(ProxyCredential.id)))
    chosen=rows[0] if rows else None
    # Existing duplicates are retired; standalone purchases are never touched.
    for duplicate in rows[1:]:
        if duplicate.status not in ('disabled','expired'):
            await service.api(await service.server(duplicate.server_id)).disable_user(duplicate.remote_username)
        duplicate.status='disabled'
        await repo.set_setting(f'proxy_included_duplicate:{duplicate.id}','1')
    if not eligible:
        if chosen and chosen.status=='active':
            await service.api(await service.server(chosen.server_id)).disable_user(chosen.remote_username)
            chosen.status='expired';await repo.set_setting(f'proxy_sub_suspended:{chosen.id}','1')
        return chosen
    owner_sub=eligible[0];expiry=utc(owner_sub.expires_at)
    if chosen:
        if utc(chosen.expires_at)!=expiry or chosen.status!='active' or await repo.setting(f'proxy_limits209:{chosen.id}')!='1':
            await service.api(await service.server(chosen.server_id)).patch_user(chosen.remote_username,expiration_rfc3339=expiry.isoformat().replace('+00:00','Z'),enabled=True,max_unique_ips=2,max_tcp_conns=2)
            chosen.expires_at=expiry;chosen.status='active'
            await repo.set_setting(f'proxy_limits209:{chosen.id}','1')
        m=await meta(repo,chosen);m.subscription_id=owner_sub.id;m.ip_limit=2
        await repo.set_setting(f'proxy_sub_suspended:{chosen.id}','0')
        return chosen
    # Historical/deleted gifts and conversions must not permanently consume
    # an account entitlement. The live included row above is the sole guard;
    # user lock + stable Telemt username make retries reuse the same access.
    if cfg['auto_included'] and create:
        servers=await service.matching_servers('vpn')
        if not servers:raise ValueError('Нет проверенного сервера со свободной ёмкостью')
        c,link=await service.provision(servers[0],user,expiry,2,2,'vpn',connection_token='included')
        m=await meta(repo,c);m.source='included';m.subscription_id=owner_sub.id
        await repo.set_setting(f'proxy_limits209:{c.id}','1')
        await notify(repo,settings,user,c,'activated',included_subscription=owner_sub)
        return c
    return None
async def gift_visible(repo,sub,receipt=False):
    # The gift belongs to the account's first paid VPN, not to every key.
    first=await repo.session.scalar(select(Subscription.id).where(Subscription.user_id==sub.user_id,Subscription.kind.in_(('paid','admin'))).order_by(Subscription.id).limit(1))
    if first!=sub.id:return False
    marker=f'proxy_gift_receipt:{sub.user_id}'
    if receipt:
        if await repo.setting(marker)=='1':return False
        await repo.set_setting(marker,'1')
    return True

async def queue_included(repo,sub):await repo.set_setting(f'proxy_included_pending:{sub.id}',now().isoformat())
async def maintenance(repo,settings):
    from app.services.telemt import TelemtError
    pending=list(await repo.session.scalars(select(AppSetting).where(AppSetting.key.startswith('proxy_included_pending:')).limit(50)))
    for p in pending:
        sub=await repo.session.get(Subscription,int(p.key.split(':')[1]))
        try:
            if sub:await sync_included(repo,settings,sub,True)
            p.value='done';await repo.session.delete(p)
        except (ValueError,TelemtError):pass
    ids=list(await repo.session.scalars(select(ProxyCredential.user_id).join(ProxyAccessDetails,ProxyAccessDetails.credential_id==ProxyCredential.id).where(ProxyAccessDetails.source=='included',ProxyCredential.status.not_in(('deleted','migrated'))).distinct()))
    from types import SimpleNamespace
    for uid in ids:
        try:
            # Reconcile the owner even if the original key was deleted or transferred.
            await sync_included(repo,settings,SimpleNamespace(user_id=uid))
        except (ValueError,TelemtError):pass
    await maintenance_tail(repo,settings)

async def maintenance_tail(repo,settings):
    cfg=await config(repo)
    # Bounded observation retention; daily aggregates outlive raw IP observations.
    from sqlalchemy import delete
    await repo.session.execute(delete(ProxyIPSeen).where(ProxyIPSeen.active.is_(False),ProxyIPSeen.last_seen<now()-timedelta(days=90)))
    await repo.session.execute(delete(ProxyDaily).where(ProxyDaily.day<(now()-timedelta(days=1095)).strftime('%Y-%m-%d')))
    if cfg['notify_expiry']:
        rows=await repo.session.scalars(select(ProxyCredential).join(ProxyAccessDetails,ProxyAccessDetails.credential_id==ProxyCredential.id).where(ProxyAccessDetails.source!='included',ProxyCredential.status.not_in(('deleted','migrated')),ProxyCredential.expires_at>now()-timedelta(days=1),ProxyCredential.expires_at<now()+timedelta(days=3)))
        for c in rows:
            from app.services.reminder_policy import stage
            days=(utc(c.expires_at)-now()).total_seconds()/86400
            due=stage(c.expires_at,now())
            if due in cfg['reminder_days'] or -1<=days<0:
                user=await repo.session.get(User,c.user_id)
                if user:await notify(repo,settings,user,c,'expired' if days<0 else f'expiry:{due}')
async def worker_transaction(session):
    # Only the background worker uses this deadline; customer payments retain
    # their existing transaction and provider verification semantics.
    if session.bind.dialect.name=='postgresql':
        from sqlalchemy import text
        await session.execute(text("SET LOCAL lock_timeout = '750ms'"))

async def maintenance_worker(database,settings):
    from app.database.repositories import SQLRepository
    from types import SimpleNamespace
    import logging
    async with database.sessions() as session:
        pending=list(await session.scalars(select(AppSetting.key).where(AppSetting.key.startswith('proxy_included_pending:')).limit(50)))
        sids=[int(key.split(':')[1]) for key in pending if key.split(':')[1].isdigit()]
        subs=list((await session.execute(select(Subscription.id,Subscription.user_id).where(Subscription.id.in_(sids)))).all())
        jobs={}
        for sid,uid in subs:jobs.setdefault(uid,[]).append(sid)
        owners=set(await session.scalars(select(ProxyCredential.user_id).join(ProxyAccessDetails,ProxyAccessDetails.credential_id==ProxyCredential.id).where(ProxyAccessDetails.source=='included',ProxyCredential.status.not_in(('deleted','migrated'))).distinct()))
        owners.update(jobs)
        orphaned=set(sids)-{sid for sid,_ in subs}
    for uid in sorted(owners):
        try:
            async with database.sessions.begin() as session:
                await worker_transaction(session)
                # Do not queue behind a customer using the bot/site. This owner
                # will be retried on the next pass with its jobs still intact.
                owner=await session.scalar(select(User).where(User.id==uid).with_for_update(skip_locked=True))
                if owner is None:continue
                repo=SQLRepository(session)
                await sync_included(repo,settings,SimpleNamespace(user_id=uid),bool(jobs.get(uid)),api_timeout=3)
                for sid in jobs.get(uid,[]):
                    row=await session.get(AppSetting,f'proxy_included_pending:{sid}')
                    if row:await session.delete(row)
        except Exception as error:
            logging.getLogger(__name__).warning('Proxy owner reconciliation postponed: %s',type(error).__name__)
    async with database.sessions.begin() as session:
        await worker_transaction(session)
        for sid in orphaned:
            row=await session.get(AppSetting,f'proxy_included_pending:{sid}')
            if row:await session.delete(row)
        await maintenance_tail(SQLRepository(session),settings)

async def proxy_gift_screen(repo, target='p:home', *, demo=False):
    from app.services.proxies import default_proxy_photo
    from app.services.screens import Screen

    media_type=await repo.setting('media_type:proxy') or 'photo'
    photo=await repo.setting('media_file:proxy') or await repo.setting('image:proxy')
    if media_type!='photo' or not photo:
        photo=default_proxy_photo()
    text=('🎁 <b>Вам подарок</b>\n\n'
          'К вашей VPN-подписке прилагается <b>бесплатный Telegram Proxy</b>. '
          'Подключите его, чтобы пользоваться Telegram без включённого VPN.\n\n'
          '<blockquote>Один прокси на аккаунт. Доступен, пока действует ваша VPN-подписка.</blockquote>')
    if demo:
        text='<b>🧪 Тестовое уведомление</b>\n\n'+text
    return Screen(text,[[('🌐 Подключить прокси',target)]],photo=photo,structured=True,
                  button_options={target:{'style':'primary'}})


async def queue_proxy_gift(repo,user,subscription):
    if subscription.kind!='paid' or not user.telegram_id or user.telegram_id<=0:
        return
    # Included provisioning already holds the customer lock. One notice per account.
    dedup=f'proxy.gift:{user.id}'
    if await repo.session.scalar(select(ChatNotice.id).where(ChatNotice.dedup==dedup)):
        return
    await repo.add(ChatNotice(dedup=dedup,target_id=user.telegram_id,kind='proxy_gift',
                              text='Вам подарок',available_at=now()+timedelta(minutes=15)))


async def current_proxy_gift(repo,settings,job):
    from cryptography.fernet import InvalidToken

    user=await repo.session.get(User,int(job.dedup.rsplit(':',1)[1]))
    if not user or user.status!='active' or user.telegram_id!=job.target_id:
        return None
    if await repo.setting(f'notifications:{user.id}','1')!='1':
        return None
    cfg=await config(repo)
    if not cfg['auto_included'] or not cfg['notify_activation']:
        return None
    result=await repo.session.execute(
        select(ProxyCredential,ProxyAccessDetails).join(ProxyAccessDetails,
            ProxyAccessDetails.credential_id==ProxyCredential.id).where(
            ProxyCredential.user_id==user.id,proxy_active(),ProxyAccessDetails.source=='included'
        ).order_by(ProxyCredential.id).limit(1))
    row=result.first()
    if not row:
        return None
    proxy,details=row
    if proxy.current_connections or proxy.active_unique_ips or proxy.total_octets:
        return None
    if await repo.session.scalar(select(ProxyIPSeen.id).where(ProxyIPSeen.credential_id==proxy.id).limit(1)):
        return None
    sub=await repo.session.get(Subscription,details.subscription_id) if details.subscription_id else None
    if not sub or sub.user_id!=user.id or not await subscription_eligible(repo,sub):
        return None
    if not proxy.link:
        return None
    target=f'p:access:{proxy.id}'
    try:
        link=ProxyService.public_link(ProxyService(repo,settings).decrypt(proxy.link))
        if link.startswith('https://t.me/proxy?'):
            target='url:'+link
    except (InvalidToken, ValueError):
        pass
    return await proxy_gift_screen(repo,target)


async def notify(repo,settings,user,c,event,*,included_subscription=None):
    cfg=await config(repo)
    if event=='activated' and not cfg['notify_activation']:return
    if await repo.setting(f'notifications:{user.id}','1')!='1' or user.status=='blocked':return
    if included_subscription is not None:
        await queue_proxy_gift(repo,user,included_subscription)
    text=f'Telegram Proxy #{c.id}: '+('доступ активирован' if event=='activated' else 'срок доступа закончился' if event=='expired' else f'срок заканчивается {utc(c.expires_at):%d.%m.%Y}')
    dedup=f'proxy_notice:{c.id}:{event}:{utc(c.expires_at).isoformat()}'
    if await repo.setting(dedup):return
    if event=='expiry:3' and await repo.setting(f'proxy_notice:{c.id}:expiry:1:{utc(c.expires_at).isoformat()}'):return
    await repo.set_setting(dedup,'1')
    # Reuse the existing message campaign/outboxes; no second notification transport.
    from app.services.offers import contacts,CHANNELS
    campaign=await repo.add(Campaign(actor_id=0,audience='proxy_notice',text=text,status='queued'))
    offer=await repo.add(PersonalOffer(user_id=user.id,campaign_id=campaign.id,created_by=user.id,request_id=hashlib.sha256(dedup.encode()).hexdigest(),fingerprint=hashlib.sha256(text.encode()).hexdigest(),discount_type='fixed',value=0,target='message',title='Telegram Proxy',message=text,expires_at=now()+timedelta(days=7)))
    await repo.set_setting(f'proxy_notice_offer:{offer.id}',json.dumps(dict(credential_id=c.id,expiry=utc(c.expires_at).isoformat(),event=event)))
    for contact in await contacts(repo,settings,user):
        if contact['available'] and not (included_subscription is not None and contact['channel']=='telegram'):
            result=await CHANNELS[contact['channel']].enqueue(repo,settings,offer,contact)
            await repo.add(OfferDelivery(offer_id=offer.id,contact_id=contact['id'],channel=contact['channel'],destination=contact['destination'],**result))
async def plans(repo):
    values=list(await repo.session.scalars(select(ProxyPlan).order_by(ProxyPlan.id)))
    if not values and await repo.settings_prefix('catalog_proxy:'):return []
    if not values:values=[await repo.add(ProxyPlan(name='Telegram Proxy',price=int(await repo.setting('proxy_monthly_price','3000')),ip_limit=(await config(repo))['default_ip_limit']))]
    result=[]
    for plan in values:
        metadata=json.loads(await repo.setting(f'catalog_proxy:{plan.id}','{}'))
        if not metadata.get('deleted'):result.append(plan)
    return result
async def quote_proxy(repo,uid,plan,renewal=False):
    from app.services.offers import discount_price
    from app.services.catalog import public_price
    best=None;price=await public_price(repo,'proxy',plan.id,plan.price,renewal)
    for o in await repo.session.scalars(select(PersonalOffer).where(PersonalOffer.user_id==uid,PersonalOffer.target=='proxy',PersonalOffer.status=='active',PersonalOffer.expires_at>now(),PersonalOffer.reserved_payment_id.is_(None)).order_by(PersonalOffer.id)):
        ids=json.loads(o.tariff_ids)
        if ids and plan.id not in ids:continue
        p=discount_price(o,plan.price)
        if p<price:best,price=o,p
    return price,best
async def record_paid(repo,settings,c,payment,order,renewal=False):
    m=await meta(repo,c)
    # A purchased extension of the gift keeps this connection independent of VPN.
    # Preserve the original one-gift-per-account history after source conversion.
    if m.source=='included':await repo.set_setting(f'proxy_included_converted:{c.user_id}',str(c.id))
    m.source='standalone';m.payment_id=payment.id;m.plan_id=order.get('plan_id');m.subscription_id=None;m.ip_limit=order['limit']
    await repo.set_setting(f'proxy_sale:{payment.id}',json.dumps(dict(credential_id=c.id,plan_id=m.plan_id,days=order['days'],ip_limit=m.ip_limit,price=payment.amount,base_price=order.get('base_price',payment.amount),discount=order.get('base_price',payment.amount)-payment.amount,renewal=renewal)))
    from app.web.admin_reports import record_sale
    await record_sale(repo,payment,0,order['limit'],kind='proxy',renewal=renewal)
    from app.services.offers import consume
    await consume(repo,payment)
    await notify(repo,settings,await repo.session.get(User,c.user_id),c,'activated')
async def reserve_offer(repo,payment,offer,base):
    if offer:
        offer.reserved_payment_id=payment.id;offer.discount_amount=base-payment.amount
async def page_data(repo,settings,tab='servers',q='',page=1,limit=10,status='all',server_id=0,source='all',capacity='all',country='',provider='',issuance='all',days=30,user_id=0,start='',end=''):
    # Small infrastructure list only; credentials and customers are always paginated by SQL.
    servers=list(await repo.session.scalars(select(ProxyServer).where(ProxyServer.status!='deleted').order_by(ProxyServer.priority,ProxyServer.id)))
    stats=list((await repo.session.execute(select(ProxyCredential.server_id,func.count(ProxyCredential.id).label('count'),func.count(func.distinct(ProxyCredential.user_id)).label('clients')).where(ProxyCredential.status.not_in(('deleted','migrated'))).group_by(ProxyCredential.server_id))).mappings())
    counts={x['server_id']:x for x in stats};lo=(now()-timedelta(days=max(0,days-1))).astimezone(ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d')
    hi=now().astimezone(ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d')
    if start and end:
        a=datetime.fromisoformat(start).date();b=datetime.fromisoformat(end).date()
        if a>b or (b-a).days>3660:raise ValueError('Проверьте период')
        lo=a.isoformat();hi=b.isoformat()
    traf={x[0]:int(x[1] or 0) for x in await repo.session.execute(select(ProxyDaily.server_id,func.sum(ProxyDaily.octets)).where(ProxyDaily.day>=lo,ProxyDaily.day<=hi).group_by(ProxyDaily.server_id))}
    connected={r[0]:r[1] for r in await repo.session.execute(select(ProxyCredential.server_id,func.count(func.distinct(ProxyCredential.user_id))).where(proxy_active(),ProxyCredential.current_connections>0).group_by(ProxyCredential.server_id))}
    formatted=[]
    for s in servers:
        opts=await server_config(repo,s.id);v=counts.get(s.id,{});n=v.get('count',0);h='maintenance' if s.status=='maintenance' else s.health if s.health in ('online','offline','degraded') else 'unknown'
        if h=='online' and not fresh(s):h='unknown'
        formatted.append(dict(id=s.id,name=s.name,status=s.status,health=h,error=s.last_error,last_sync=utc(s.last_sync).isoformat() if s.last_sync else None,host=opts.get('host') or urlparse(s.api_url).hostname,port=opts['port'],country=opts['country'],city=opts['city'],provider=opts['provider'],audience=s.audience,ip_limit=s.unique_ip_limit,tcp_limit=s.tcp_connection_limit,priority=s.priority,capacity=n,max_accounts=opts['max_accounts'],clients=v.get('clients',0),online=connected.get(s.id,0) if fresh(s) else None,traffic=traf.get(s.id) if s.last_sync else None,traffic_total=s.total_octets if s.last_sync else None,notes=opts['notes'],max_clients=opts['max_clients'],traffic_limit=opts['traffic_limit'],created_at=utc(s.created_at).isoformat(),auth_saved=bool(s.auth),api_url=s.api_url))
    active=await repo.session.scalar(select(func.count(ProxyCredential.id)).where(proxy_active()))
    customers=await repo.session.scalar(select(func.count(func.distinct(ProxyCredential.user_id))).where(ProxyCredential.status.not_in(('deleted','migrated'))))
    total=await repo.session.scalar(select(func.count(ProxyCredential.id)).where(ProxyCredential.status.not_in(('deleted','migrated'))))
    sales=list(await repo.session.scalars(select(Payment).where(Payment.status=='proxy_order_paid',Payment.created_at>=datetime.fromisoformat(lo).replace(tzinfo=ZoneInfo('Europe/Moscow')),Payment.created_at<datetime.fromisoformat(hi).replace(tzinfo=ZoneInfo('Europe/Moscow'))+timedelta(days=1))))
    balance=await repo.session.scalar(select(func.sum(-BalanceTransaction.amount)).where(BalanceTransaction.type=='proxy_purchase',BalanceTransaction.amount<0,BalanceTransaction.created_at>now()-timedelta(days=days)))
    summary=dict(servers=len(servers),healthy=sum(s['health']=='online' for s in formatted),proxies=total,active=active,clients=customers,traffic=sum(traf.values()) if any(s.last_sync for s in servers) else None,sales=len(sales),revenue=sum(p.amount for p in sales)+int(balance or 0),online=await repo.session.scalar(select(func.count(func.distinct(ProxyCredential.user_id))).where(proxy_active(),ProxyCredential.current_connections>0,ProxyCredential.server_id.in_([s.id for s in servers if fresh(s)]))) if any(fresh(s) for s in servers) else None)
    types={r[0]:r[1] for r in await repo.session.execute(select(ProxyAccessDetails.source,func.count(ProxyAccessDetails.id)).join(ProxyCredential,ProxyCredential.id==ProxyAccessDetails.credential_id).where(ProxyCredential.status.not_in(('deleted','migrated'))).group_by(ProxyAccessDetails.source))}
    summary.update(included=types.get('included',0),standalone=types.get('standalone',0),administrative=types.get('admin',0),expired=await repo.session.scalar(select(func.count()).select_from(ProxyCredential).where(ProxyCredential.expires_at<=now(),ProxyCredential.status.not_in(('deleted','migrated')))),expiring=await repo.session.scalar(select(func.count()).select_from(ProxyCredential).where(proxy_active(),ProxyCredential.expires_at<now()+timedelta(days=7))))
    from app.web.admin_reports import build_report
    if repo.session.bind.dialect.name=='postgresql':
        report=await build_report(repo.session,datetime.fromisoformat(lo).replace(tzinfo=ZoneInfo('Europe/Moscow')),datetime.fromisoformat(hi).replace(tzinfo=ZoneInfo('Europe/Moscow'))+timedelta(days=1),kind='proxy',limit=1)
        summary.update(sales=report['stats']['sales'],revenue=report['stats']['revenue'],renewals=report['stats']['renewals'],average=report['stats']['average'],refunds=None)
    if tab=='servers':
        rows=[s for s in formatted if (not q or q.lower() in ' '.join(str(s[k]) for k in ('name','id','host','city','country','provider','api_url')).lower()) and (status=='all' or s['health']==status) and (not country or s['country']==country) and (not provider or s['provider']==provider) and (issuance=='all' or (s['status']=='active')==(issuance=='enabled'))]
        def cap(s):
            x=s['capacity']/max(1,s['max_accounts']);return capacity=='all' or capacity=='low' and x<.5 or capacity=='medium' and .5<=x<=.8 or capacity=='high' and x>.8 or capacity=='full' and x>=1
        rows=[s for s in rows if cap(s)];count=len(rows);items=rows[(page-1)*limit:page*limit]
    elif tab in ('proxies','clients'):
        state=case((ProxyCredential.status.in_(('disabled','blocked')),'blocked'),(ProxyCredential.expires_at<=now(),'expired'),(ProxyCredential.expires_at<now()+timedelta(days=7),'expiring'),else_='active')
        query=select(ProxyCredential,User,ProxyAccessDetails,ProxyServer).join(User,User.id==ProxyCredential.user_id).join(ProxyServer,ProxyServer.id==ProxyCredential.server_id).outerjoin(ProxyAccessDetails,ProxyAccessDetails.credential_id==ProxyCredential.id).where(ProxyCredential.status.not_in(('deleted','migrated')))
        if user_id:query=query.where(User.id==user_id)
        if q:
            term='%'+q.replace('%','\\%').replace('_','\\_')+'%';query=query.where(or_(User.first_name.ilike(term,escape='\\'),User.username.ilike(term,escape='\\'),User.freeman_id.ilike(term,escape='\\'),User.email.ilike(term,escape='\\'),ProxyCredential.remote_username.ilike(term,escape='\\'),ProxyCredential.id==int(q) if q.isdigit() else False))
        if status!='all':query=query.where(state==status)
        if server_id:query=query.where(ProxyCredential.server_id==server_id)
        if source!='all':query=query.where(ProxyAccessDetails.source==source)
        if tab=='clients':
            ids=query.with_only_columns(User.id).distinct().subquery();count=await repo.session.scalar(select(func.count()).select_from(ids))
            uids=list(await repo.session.scalars(select(ids.c.id).order_by(ids.c.id.desc()).offset((page-1)*limit).limit(limit)))
            query=query.where(User.id.in_(uids))
        else:count=await repo.session.scalar(select(func.count()).select_from(query.subquery()));query=query.order_by(ProxyCredential.id.desc()).offset((page-1)*limit).limit(limit)
        items=[]
        for c,u,m,s in await repo.session.execute(query):
            st='blocked' if c.status in ('disabled','blocked') else 'missing' if c.status=='missing' else 'expired' if utc(c.expires_at)<=now() else 'expiring' if utc(c.expires_at)<now()+timedelta(days=7) else 'active'
            items.append(dict(id=c.id,user_id=u.id,name=u.display_name or u.username or u.freeman_id,freeman_id=u.freeman_id,username=u.username,avatar=f'/api/web/admin/users/{u.id}/avatar' if u.telegram_id else None,server_id=s.id,server=s.name,source=m.source if m else 'unknown',subscription_id=m.subscription_id if m else None,payment_id=m.payment_id if m else None,ip_limit=m.ip_limit if m else None,ips=c.active_unique_ips if fresh(s) else None,online=c.current_connections>0 if fresh(s) else None,traffic=c.total_octets if c.last_sync else None,status=st,expires_at=utc(c.expires_at).isoformat(),created_at=utc(c.created_at).isoformat()))
        if tab=='clients':
            grouped={}
            for x in items:
                uid=x['user_id'];v=grouped.setdefault(uid,dict(id=uid,name=x['name'],freeman_id=x['freeman_id'],avatar=x['avatar'],keys=[],active=0,traffic=0,online=None,expires_at=x['expires_at']))
                v['keys'].append(x);v['active']+=x['status'] in ('active','expiring');v['traffic']+=x['traffic'] or 0
                if x['online'] is not None:v['online']=bool(v['online'] or x['online'])
                v['expires_at']=max(v['expires_at'],x['expires_at'])
            items=list(grouped.values())
    else:items=[];count=0
    series=[dict(day=r[0],traffic=int(r[1])) for r in await repo.session.execute(select(ProxyDaily.day,func.sum(ProxyDaily.octets)).where(ProxyDaily.day>=lo,ProxyDaily.day<=hi).group_by(ProxyDaily.day).order_by(ProxyDaily.day))]
    recent=[dict(ip=i.ip,first_seen=utc(i.created_at).isoformat(),last_seen=utc(i.last_seen).isoformat(),active=i.active,credential_id=c.id,user_id=u.id,name=u.display_name,freeman_id=u.freeman_id,server=s.name) for i,c,u,s in await repo.session.execute(select(ProxyIPSeen,ProxyCredential,User,ProxyServer).join(ProxyCredential,ProxyCredential.id==ProxyIPSeen.credential_id).join(User,User.id==ProxyCredential.user_id).join(ProxyServer,ProxyServer.id==ProxyCredential.server_id).order_by(ProxyIPSeen.last_seen.desc()).limit(10))]
    return dict(items=items,total=count,page=page,limit=limit,servers=formatted,summary=summary,series=series,recent=recent,telemetry_note='Трафик за период учитывается с момента подключения мониторинга; IP — наблюдения API, без выдуманной длительности сессии.')

async def notice_current(repo,offer):
    raw=await repo.setting(f'proxy_notice_offer:{offer.id}')
    if not raw:return True
    try:data=json.loads(raw)
    except (ValueError,TypeError):return False
    credential=await repo.session.get(ProxyCredential,data.get('credential_id'))
    if not credential or credential.user_id!=offer.user_id or credential.status in ('deleted','migrated','disabled','blocked'):return False
    if utc(credential.expires_at).isoformat()!=data.get('expiry'):return False
    event=data.get('event','')
    from app.services.reminder_policy import stage
    if event.startswith('expiry:'):return event in ('expiry:1','expiry:3') and stage(credential.expires_at,now())==int(event.split(':')[1])
    if event=='expired':return -86400<(utc(credential.expires_at)-now()).total_seconds()<=0
    return event=='activated'
