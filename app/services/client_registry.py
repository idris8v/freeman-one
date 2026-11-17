"""One server-side filter engine for the CRM, export and campaign audience."""
from datetime import timedelta,timezone,datetime
from sqlalchemy import select,func,case,or_,and_,cast,String,BigInteger,literal,union_all
from app.database.models import User,Payment,AccessKey,ProxyCredential,Subscription,VlessCredential,VpnCredential,Device,AppSetting,CustomerContact,Ticket,TicketMessage,now
from app.database.repositories import SQLRepository

# Subscription controls participate in every active-key count. Historical keys
# do not override another valid active subscription belonging to the same client.
def active_key(key=AccessKey):
 links=select(VlessCredential.subscription_id.label('sid')).where(VlessCredential.access_key_id==key.id).correlate(key).union(select(VpnCredential.subscription_id).where(VpnCredential.access_key_id==key.id).correlate(key),select(Subscription.id).join(AppSetting,AppSetting.key==literal('subscription_key:')+cast(Subscription.id,String)).where(AppSetting.value==cast(key.id,String)).correlate(key)).subquery()
 stopped=select(AppSetting.key).select_from(links).join(AppSetting,or_(and_(AppSetting.key==literal('subscription_status:')+cast(links.c.sid,String),AppSetting.value!='active'),and_(AppSetting.key==literal('subscription_removed:')+cast(links.c.sid,String),AppSetting.value=='1'),and_(AppSetting.key==literal('subscription_transferred:')+cast(links.c.sid,String),AppSetting.value=='1'))).exists()
 return and_(key.status=='active',key.expires_at>now(),~stopped)

def visible_customer():
 # Checkout placeholders are not registered customers until contact or service exists.
 return or_(User.registration_source!='website_guest', User.email.is_not(None) & (User.email!=''), User.telegram_id>0,
  select(Subscription.id).where(Subscription.user_id==User.id).exists(),
  select(AccessKey.id).where(AccessKey.user_id==User.id).exists(),
  select(ProxyCredential.id).where(ProxyCredential.user_id==User.id).exists(),
  select(Payment.id).where(Payment.user_id==User.id,Payment.status.in_(('succeeded','order_paid','balance_paid','stars_paid','manual_paid','proxy_order_paid','refunded'))).exists())

async def query(session,settings,f,user_ids=None):
 scoped=lambda col: literal(True) if user_ids is None else col.in_(user_ids)
 active=active_key()
 ks=select(AccessKey.user_id.label('uid'),func.count().label('total'),func.sum(case((active,1),else_=0)).label('active'),func.min(case((active,AccessKey.expires_at))).label('nearest'),func.max(case((AccessKey.expires_at<=now(),AccessKey.expires_at))).label('expired')).where(AccessKey.status!='deleted',scoped(AccessKey.user_id)).group_by(AccessKey.user_id).subquery()
 ps=select(ProxyCredential.user_id.label('uid'),func.sum(case((and_(ProxyCredential.status=='active',ProxyCredential.expires_at>now()),1),else_=0)).label('active'),func.count().label('total'),func.sum(case((ProxyCredential.expires_at<=now(),1),else_=0)).label('expired')).where(ProxyCredential.status!='deleted',scoped(ProxyCredential.user_id)).group_by(ProxyCredential.user_id).subquery()
 ds=select(Subscription.user_id.label('uid'),Subscription.id.label('sid'),Subscription.device_limit.label('capacity'),func.count(Device.id).label('count')).join(Device,Device.subscription_id==Subscription.id).where(Device.status.not_in(('removed','deleted','disconnected')),scoped(Subscription.user_id)).group_by(Subscription.user_id,Subscription.id,Subscription.device_limit).subquery()
 dc=select(ds.c.uid,func.sum(ds.c.count).label('connected'),func.max(case((ds.c.count>=ds.c.capacity,1),else_=0)).label('at_limit')).group_by(ds.c.uid).subquery()
 epoch=lambda col:cast(func.strftime('%s',col),BigInteger) if session.bind.dialect.name=='sqlite' else cast(func.extract('epoch',col),BigInteger)
 activity=union_all(select(cast(func.substr(AppSetting.key,13),BigInteger).label('uid'),cast(AppSetting.value,BigInteger).label('at')).where(AppSetting.key.like('client_seen:%'),scoped(cast(func.substr(AppSetting.key,13),BigInteger))),select(Ticket.user_id,epoch(TicketMessage.created_at)).join(TicketMessage,TicketMessage.ticket_id==Ticket.id).where(TicketMessage.role=='client',scoped(Ticket.user_id)),select(VpnCredential.user_id,epoch(VpnCredential.last_handshake)).where(VpnCredential.last_handshake.is_not(None),scoped(VpnCredential.user_id))).subquery()
 seen=select(activity.c.uid,func.max(activity.c.at).label('at')).group_by(activity.c.uid).subquery()
 ac=func.coalesce(ks.c.active,0);pc=func.coalesce(ps.c.active,0);connected=func.coalesce(dc.c.connected,0)
 state=case((User.status=='blocked','blocked'),(ac+pc>0,'active'),(and_(seen.c.at.is_not(None),seen.c.at<int((now()-timedelta(days=30)).timestamp())),'inactive'),else_='no_services')
 fields=dict(q=f.q.strip(),status=f.status,origin=f.origin,key_state=f.key_state,activity=f.activity,devices=f.devices,proxy=f.proxy,contact=f.contact,sort=f.sort)
 allowed=dict(status=['all','active','inactive','blocked','no_services'],origin=['all','external','internal'],key_state=['all','active','none','one','multiple','soon3','soon7','expired','expired7','expired30','expired90'],activity=['all','today','recent7','idle7','idle30','idle90','idle30plus'],devices=['all','connected','none','limit'],proxy=['all','has','none','active','expired'],contact=['all','telegram','email','whatsapp','none'],sort=['newest','name','activity','active_keys','expiry','status'])
 for k,values in allowed.items():
  if fields[k] not in values:raise ValueError('Некорректный фильтр: '+k)
 conditions=[visible_customer(),scoped(User.id)]
 if fields['status']!='all':conditions.append(state==fields['status'])
 if fields['origin']=='external':conditions.append(User.registration_source=='external')
 if fields['origin']=='internal':conditions.append(User.registration_source!='external')
 if fields['q']:
  q=fields['q'].lstrip('@');term='%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
  from app.services.external_contacts import find
  phone_uid=await find(SQLRepository(session),settings,q)
  parts=[User.first_name.ilike(term,escape='\\'),User.username.ilike(term,escape='\\'),User.email.ilike(term,escape='\\'),User.freeman_id.ilike(term,escape='\\'),User.id==phone_uid if phone_uid else literal(False),select(CustomerContact.id).where(CustomerContact.user_id==User.id,CustomerContact.destination.ilike(term,escape='\\')).exists(),select(AccessKey.id).where(AccessKey.user_id==User.id,or_(AccessKey.name.ilike(term,escape='\\'),cast(AccessKey.id,String)==q)).exists(),select(VlessCredential.id).where(VlessCredential.user_id==User.id,or_(VlessCredential.subscription_url.ilike(term,escape='\\'),cast(VlessCredential.subscription_id,String)==q)).exists(),select(ProxyCredential.id).where(ProxyCredential.user_id==User.id,ProxyCredential.link.ilike(term,escape='\\')).exists()]
  from app.services.accounts import find_freeman_id
  identity_user=await find_freeman_id(session,q)
  if identity_user:parts.append(User.id==identity_user.id)
  if q.isdigit():parts.extend([User.id==int(q),User.telegram_id==int(q)])
  conditions.append(or_(*parts))
 k=fields['key_state']
 if k in ('active','none','one','multiple'):conditions.append({'active':ac>0,'none':ac==0,'one':ac==1,'multiple':ac>1}[k])
 if k in ('soon3','soon7'):conditions.append(ks.c.nearest<=now()+timedelta(days=3 if k=='soon3' else 7))
 if k.startswith('expired'):
  conditions.extend([ac==0,ks.c.expired.is_not(None)])
  ranges={'expired7':(7,30),'expired30':(30,90),'expired90':(90,None)}
  if k in ranges:
   lo,hi=ranges[k];conditions.append(ks.c.expired<=now()-timedelta(days=lo))
   if hi:conditions.append(ks.c.expired>now()-timedelta(days=hi))
 a=fields['activity']
 if a!='all':
  if a=='today':
   moscow=now().astimezone(timezone(timedelta(hours=3))).replace(hour=0,minute=0,second=0,microsecond=0);conditions.append(seen.c.at>=int(moscow.timestamp()))
  elif a=='recent7':conditions.append(seen.c.at>=int((now()-timedelta(days=7)).timestamp()))
  else:
   lo,hi={'idle7':(7,30),'idle30':(30,90),'idle90':(90,None),'idle30plus':(30,None)}[a];conditions.append(seen.c.at<int((now()-timedelta(days=lo)).timestamp()))
   if hi:conditions.append(seen.c.at>=int((now()-timedelta(days=hi)).timestamp()))
 if fields['devices']!='all':conditions.append({'connected':connected>0,'none':connected==0,'limit':dc.c.at_limit==1}[fields['devices']])
 if fields['proxy']!='all':conditions.append({'has':func.coalesce(ps.c.total,0)>0,'none':func.coalesce(ps.c.total,0)==0,'active':pc>0,'expired':func.coalesce(ps.c.expired,0)>0}[fields['proxy']])
 external=select(CustomerContact.id).where(CustomerContact.user_id==User.id,CustomerContact.enabled.is_(True))
 contact_conditions={'telegram':or_(User.telegram_id.is_not(None),external.where(CustomerContact.channel=='telegram').exists()),'email':or_(User.email.is_not(None),external.where(CustomerContact.channel=='email').exists()),'whatsapp':external.where(CustomerContact.channel=='whatsapp').exists(),'none':and_(User.telegram_id.is_(None),User.email.is_(None),~external.exists())}
 if fields['contact']!='all':conditions.append(contact_conditions[fields['contact']])
 stmt=select(User,ac.label('active_keys'),func.coalesce(ks.c.total,0).label('total_keys'),pc.label('active_proxy'),seen.c.at.label('last_activity'),ks.c.nearest.label('nearest'),ks.c.expired.label('expired'),state.label('customer_status')).outerjoin(ks,ks.c.uid==User.id).outerjoin(ps,ps.c.uid==User.id).outerjoin(dc,dc.c.uid==User.id).outerjoin(seen,seen.c.uid==User.id).where(*conditions)
 order={'newest':User.id.desc(),'name':func.lower(User.first_name),'activity':seen.c.at.desc().nullslast(),'active_keys':ac.desc(),'expiry':ks.c.nearest.asc().nullslast(),'status':state}[fields['sort']]
 return stmt.order_by(order,User.id.desc())

async def listing(session,settings,f):
 fast=not f.q.strip() and f.sort in ('newest','name') and all(getattr(f,k)=='all' for k in ('status','key_state','activity','devices','proxy','contact')) and f.origin in ('all','internal','external')
 if fast:
  conditions=[visible_customer()]
  if f.origin!='all':conditions.append(User.registration_source=='external' if f.origin=='external' else User.registration_source!='external')
  total=await session.scalar(select(func.count()).select_from(User).where(*conditions));page=min(max(1,f.page),max(1,(total+f.limit-1)//f.limit))
  order=User.id.desc() if f.sort=='newest' else func.lower(User.first_name)
  ids=list(await session.scalars(select(User.id).where(*conditions).order_by(order,User.id.desc()).offset((page-1)*f.limit).limit(f.limit)))
  rows=(await session.execute(await query(session,settings,f,user_ids=ids))).all() if ids else []
 else:
  stmt=await query(session,settings,f);total=await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()));page=min(max(1,f.page),max(1,(total+f.limit-1)//f.limit))
  rows=(await session.execute(stmt.offset((page-1)*f.limit).limit(f.limit))).all();ids=[r[0].id for r in rows]
 contacts={uid:[] for uid in ids}
 for c in await session.scalars(select(CustomerContact).where(CustomerContact.user_id.in_(ids),CustomerContact.enabled.is_(True),~CustomerContact.destination.startswith('ai:'))):contacts[c.user_id].append(dict(channel=c.channel,label=__import__('app.services.offers',fromlist=['masked']).masked(c.channel,c.destination),destination=c.destination if c.channel=='telegram' else None))
 items=[]
 for u,ac,total_keys,pc,seen,nearest,expired,status in rows:
  cs=contacts[u.id]
  if u.telegram_id:cs.insert(0,dict(channel='telegram',label='@'+u.username if u.username else str(u.telegram_id),destination=u.username))
  if u.email:cs.append(dict(channel='email',label=u.email,destination=u.email))
  cs.append(dict(channel='internal',label='Freeman ONE'))
  items.append(dict(id=u.id,name=u.display_name or u.username or 'Клиент',freeman_id=u.freeman_id,avatar=f'/api/web/admin/users/{u.id}/avatar' if u.telegram_id else None,registration_source=u.registration_source,created_at=u.created_at.replace(tzinfo=timezone.utc).isoformat() if u.created_at else None,active_keys=ac,total_keys=total_keys,active_proxy=pc,vpn_count=ac,proxy_count=pc,contacts=cs,status=status,last_activity=datetime.fromtimestamp(seen,timezone.utc).isoformat() if seen else None,nearest_expiry=nearest.replace(tzinfo=timezone.utc).isoformat() if nearest else None,last_expiry=expired.replace(tzinfo=timezone.utc).isoformat() if expired else None))
 return dict(items=items,total=total,page=page,limit=f.limit)
