"""Explicit owner knowledge writes and bounded operational facts."""
import json,hashlib
from sqlalchemy import select
from app.database.models import AIKnowledge,AIEvent,AIMessage,RemnawaveServer,VlessCredential,Subscription,Device,ProxyServer,now
from app.database.repositories import SQLRepository
from app.services.customer_identity import lock_nonce
from .security import redact,aware
DEFS={
 'expiring_subscriptions':('Подписки, истекающие за ближайшие дни. Возвращает до 30 реальных подписок.',{'days':{'type':'string','enum':['1','3','7','14','30']}}),
 'save_knowledge':('По прямому поручению владельца сохранить новую или обновить существующую инструкцию в базе знаний. article_id пустой для новой, иначе точный ID из read_knowledge. Используй Markdown: заголовки, шаги, списки. Не помещай секреты.',{'article_id':{'type':'string','maxLength':20},'title':{'type':'string','maxLength':160},'text':{'type':'string','maxLength':6000}}),
 'server_overview':('Реальные сохранённые показатели VPN и Proxy серверов, с временем последней синхронизации. CPU/RAM не выдумывай.',{}),
 'key_detail':('Подробности VPN-подписки по её ID: владелец, срок, устройства и сохранённые счётчики трафика. Не раскрывает секретную ссылку.',{'subscription_id':{'type':'string','maxLength':20}}),
}
async def execute(s,settings,c,name,args):
 from .owner import authorize
 await authorize(s,settings,c)
 if name=='save_knowledge':
  # Only an explicit current user instruction enables writes; document/tool content cannot grant it.
  latest=await s.scalar(select(AIMessage).where(AIMessage.conversation_id==c.id,AIMessage.role=='client').order_by(AIMessage.id.desc()).limit(1))
  actions=json.loads(latest.actions) if latest else []
  if not any(a.get('type')=='knowledge_write_authorized' for a in actions):raise PermissionError('Explicit knowledge instruction required')
  title=redact(args['title']).strip();body=redact(args['text']).strip();aid=args['article_id']
  if not title or not body or (aid and not aid.isdigit()):raise ValueError('Invalid article')
  await lock_nonce(s,'ai:knowledge-write:'+str(latest.id))
  key='ai_knowledge_write:'+hashlib.sha256((str(latest.id)+json.dumps(args,sort_keys=True)).encode()).hexdigest()
  repo=SQLRepository(s);saved=await repo.setting(key,'')
  if saved:return {'saved':True,'id':int(saved),'already_saved':True}
  k=await s.get(AIKnowledge,int(aid),with_for_update=True) if aid else None
  if aid and not k:raise ValueError('Article not found')
  if k:
   await repo.set_setting(f'ai_knowledge_revision:{k.id}:{k.version}',json.dumps({'title':k.title,'text':k.text,'enabled':k.enabled}))
   k.title=title;k.text=body;k.version+=1;k.updated_at=now()
  else:k=AIKnowledge(title=title,text=body,enabled=True);s.add(k)
  await s.flush();await repo.set_setting(key,str(k.id));s.add(AIEvent(conversation_id=c.id,kind='knowledge.saved',name=str(k.id),details=json.dumps({'actor_id':c.user_id,'message_id':latest.id,'version':k.version})))
  return {'saved':True,'id':k.id,'version':k.version,'title':k.title,'actions':[{'type':'knowledge_saved','id':k.id,'title':k.title}]}
 if name=='expiring_subscriptions':
  from datetime import timedelta
  from app.database.models import User
  from .insights import customer
  rows=await s.scalars(select(Subscription).where(Subscription.expires_at>=now(),Subscription.expires_at<=now()+timedelta(days=int(args['days']))).order_by(Subscription.expires_at).limit(30))
  items=[]
  for sub in rows:
   u=await s.get(User,sub.user_id)
   if u:items.append({'id':sub.id,'client':customer(u),'expires_at':aware(sub.expires_at).isoformat(),'device_limit':sub.device_limit})
  return {'items':items,'limit':30}
 if name=='server_overview':
  vpn=list(await s.scalars(select(RemnawaveServer).order_by(RemnawaveServer.id).limit(50)));proxy=list(await s.scalars(select(ProxyServer).order_by(ProxyServer.id).limit(50)))
  return {'as_of':now().isoformat(),'vpn':[dict(id=x.id,name=x.name,country=x.country,status=x.status,health=x.health,users_total=x.users_total,users_online=x.users_online,max_users=x.max_users,traffic_bytes=x.traffic_used,last_sync=aware(x.last_sync).isoformat() if x.last_sync else None) for x in vpn],'proxy':[dict(id=x.id,name=x.name,status=x.status,health=x.health,active_users=x.active_users,current_connections=x.current_connections,traffic_bytes=x.total_octets,last_sync=aware(x.last_sync).isoformat() if x.last_sync else None) for x in proxy],'limit':50,'cpu_ram':'CPU/RAM не доступны в этих данных. Счётчики отражают последнюю синхронизацию, а не гарантированное состояние сейчас.'}
 raw=args['subscription_id']
 if not raw.isdigit():raise ValueError('Invalid ID')
 sub=await s.get(Subscription,int(raw))
 if not sub:raise ValueError('Subscription not found')
 key=await s.scalar(select(VlessCredential).where(VlessCredential.subscription_id==sub.id));devices=list(await s.scalars(select(Device).where(Device.subscription_id==sub.id).order_by(Device.id.desc()).limit(30)))
 return dict(id=sub.id,client_id=sub.user_id,kind=sub.kind,expires_at=aware(sub.expires_at).isoformat(),device_limit=sub.device_limit,credential=dict(id=key.access_key_id,server_id=key.server_id,status=key.status,traffic_used=key.traffic_used,traffic_limit=key.traffic_limit,last_sync=aware(key.last_sync).isoformat() if key.last_sync else None) if key else None,devices=[dict(id=x.id,name=redact(x.name),type=x.type,status=x.status,created_at=aware(x.created_at).isoformat()) for x in devices],note='Список устройств — сохранённые привязки, не доказательство активных соединений.')
