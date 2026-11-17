"""Owner-only management of configured Remnawave panels (3.4 API contracts).

No arbitrary URLs/HTTP paths or credentials are accepted from the browser.
Writes have a durable request marker: an ambiguous timeout cannot be replayed.
"""
import asyncio
import hashlib
import json
import re
from uuid import UUID
from typing import Literal
from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from app.database.models import RemnawaveServer, AdminAudit
from app.database.repositories import SQLRepository
from app.services.customer_identity import lock_nonce
from app.services.remnawave import RemnawaveService, RemnawaveApiError
from app.web.access import require_business_admin

FIELDS = {
 'nodes':set('name address port proxyUrl isTrafficTrackingActive trafficLimitBytes notifyPercent trafficResetDay countryCode consumptionMultiplier nodeConsumptionMultiplier configProfile providerUuid tags activePluginUuid integrationUuids note ips'.split()),
 'hosts':set('inbound remark address port path sni host alpn fingerprint isDisabled securityLayer xhttpExtraParams muxParams sockoptParams finalMask serverDescription tags isHidden overrideSniFromAddress keepSniBlank vlessRouteId pinnedPeerCertSha256 verifyPeerCertByName shuffleHost mihomoX25519 mihomoIpVersion nodes xrayJsonTemplateUuid excludeFromSubscriptionTypes mapper internalSquads'.split()),
 'config-profiles':{'name','config'},
 'internal-squads':{'name','inbounds'},
 'external-squads':set('name templates subscriptionSettings hostOverrides responseHeadersAdd responseHeadersRemove hwidSettings customRemarks subpageConfigUuid'.split()),
 'panel':{'name'},
}
COLLECTIONS={'nodes':None,'hosts':None,'config-profiles':'configProfiles','internal-squads':'internalSquads','external-squads':'externalSquads','inbounds':'inbounds'}
READ_PATHS={k:'/'+k for k in COLLECTIONS}|{'inbounds':'/config-profiles/inbounds','metadata':'/system/metadata','stats':'/system/stats'}

class PanelCommand(BaseModel):
 model_config=ConfigDict(extra='forbid')
 resource:Literal['panel','nodes','hosts','config-profiles','internal-squads','external-squads']
 action:Literal['save','delete','enable','disable','restart','restart-all','reset-traffic','clone','reorder']
 target:str=Field(default='',max_length=36)
 data:dict=Field(default_factory=dict)
 confirmed:bool=False
 request_id:str=Field(min_length=10,max_length=64,pattern=r'^[a-zA-Z0-9-]+$')

def valid_uuid(value):
 try:return str(UUID(value))
 except (ValueError,TypeError,AttributeError):raise HTTPException(422,'Некорректный UUID')

def command(p):
 if not p.confirmed:raise HTTPException(409,'Подтвердите изменение')
 if len(json.dumps(p.data,ensure_ascii=False))>200000:raise HTTPException(422,'Слишком большой объект')
 resource=p.resource;data=dict(p.data)
 if p.target:valid_uuid(p.target)
 if resource=='panel':
  if p.action!='save' or p.target or set(data)!={'name'}:raise HTTPException(422,'Можно изменить название подключения')
  if not isinstance(data['name'],str) or not 1<=len(data['name'].strip())<=80:raise HTTPException(422,'Укажите название до 80 символов')
  return None,None,{'name':data['name'].strip()}
 path='/'+resource
 if p.action=='save':
  if not data or set(data)-FIELDS[resource]:raise HTTPException(422,'Неизвестные или пустые параметры')
  label='remark' if resource=='hosts' else 'name'
  if label in data and (not isinstance(data[label],str) or not (3 if resource=='nodes' else 1 if resource=='hosts' else 2)<=len(data[label].strip())<=(100 if resource=='hosts' else 30)):raise HTTPException(422,'Проверьте название')
  if resource in ('config-profiles','internal-squads','external-squads') and 'name'in data and not re.fullmatch(r'[A-Za-z0-9_ -]+',data['name']):raise HTTPException(422,'Имя профиля или группы: латиница, цифры, пробел, дефис или подчёркивание')
  if 'port'in data and(type(data['port'])is not int or not 1<=data['port']<=65535):raise HTTPException(422,'Порт: от 1 до 65535')
  if not p.target:
   required={'nodes':{'name','address','configProfile'},'hosts':{'remark','address','port','inbound'},'config-profiles':{'name','config'},'internal-squads':{'name','inbounds'},'external-squads':{'name'}}[resource]
   if not required<=data.keys():raise HTTPException(422,'Заполните обязательные параметры')
  if 'config' in data and not isinstance(data['config'],dict):raise HTTPException(422,'Конфигурация должна быть объектом JSON')
  if p.target:data['uuid']=p.target
  return ('PATCH' if p.target else 'POST'),path,data
 if p.action=='reorder':
  field={'nodes':'nodes','hosts':'hosts'}.get(resource,'items')
  if set(data)!={field} or not isinstance(data[field],list) or not 1<=len(data[field])<=1000:raise HTTPException(422,'Проверьте порядок записей')
  ids=[]
  for row in data[field]:
   if not isinstance(row,dict) or set(row)!={'uuid','viewPosition'} or type(row['viewPosition'])is not int or row['viewPosition']<0:raise HTTPException(422,'Проверьте порядок записей')
   ids.append(valid_uuid(row['uuid']))
  if len(set(ids))!=len(ids):raise HTTPException(422,'UUID не должны повторяться')
  return 'POST',path+'/actions/reorder',data
 if data:raise HTTPException(422,'Для этого действия параметры не нужны')
 if p.action=='restart-all' and resource=='nodes' and not p.target:return 'POST',path+'/actions/restart-all',{'forceRestart':False}
 if not p.target:raise HTTPException(422,'Выберите запись')
 if p.action=='delete':return 'DELETE',path+'/'+p.target,None
 if resource=='nodes' and p.action in ('enable','disable','restart','reset-traffic'):return 'POST',path+'/'+p.target+'/actions/'+p.action,{'forceRestart':False} if p.action=='restart' else None
 if resource=='hosts' and p.action in ('enable','disable'):return 'PATCH',path,{'uuid':p.target,'isDisabled':p.action=='disable'}
 if resource=='hosts' and p.action=='clone':return 'POST',path+'/actions/clone',{'cloneFromUuid':p.target}
 raise HTTPException(422,'Действие недоступно для этого раздела')

def sanitized(value):
 if isinstance(value,list):return [sanitized(v)for v in value]
 if isinstance(value,dict):return {k:sanitized(v)for k,v in value.items() if k not in ('apiToken','api_token','token','gatewayToken','proxyUrl')}
 return value

def unwrap(value):
 if not isinstance(value,dict):raise RemnawaveApiError('Некорректный ответ API')
 result=value.get('response',value)
 if isinstance(result,dict) and any(result.get(k)is False for k in ('isOk','isUpdated','isDeleted','success')):raise RemnawaveApiError('Панель не подтвердила изменение')
 return result

def remnawave_admin_router(database,settings,current_user):
 router=APIRouter()
 async def allowed(token):return require_business_admin(await current_user(token),settings)
 async def panel(session,pid):
  item=await session.get(RemnawaveServer,pid)
  if not item:raise HTTPException(404,'Подключение Remnawave не найдено')
  return item
 @router.get('/api/web/admin/remnawave')
 async def panels(freeman_session:str|None=Cookie(default=None)):
  await allowed(freeman_session)
  async with database.sessions() as s:
   rows=await s.scalars(select(RemnawaveServer).order_by(RemnawaveServer.priority,RemnawaveServer.id))
   return {'items':[dict(id=v.id,name=v.name,country=v.country,health=v.health,last_sync=v.last_sync.isoformat()if v.last_sync else None)for v in rows]}
 @router.get('/api/web/admin/remnawave/{pid}/{resource}')
 async def read(pid:int,resource:str,item:str='',freeman_session:str|None=Cookie(default=None)):
  await allowed(freeman_session)
  if resource not in READ_PATHS:raise HTTPException(404,'Раздел недоступен')
  if item and resource not in FIELDS:raise HTTPException(422,'Параметры недоступны')
  path=READ_PATHS[resource]+('/'+valid_uuid(item)if item else '')
  try:
   async with database.sessions() as s:
    v=await panel(s,pid);api=RemnawaveService(SQLRepository(s),settings).api(v)
    result=unwrap(await api.request('GET',path))
   if item or resource not in COLLECTIONS:return {'data':sanitized(result)}
   rows=result if isinstance(result,list)else result.get(COLLECTIONS[resource],[])if isinstance(result,dict)else []
   if not isinstance(rows,list):raise RemnawaveApiError('Некорректный список API')
   return {'items':sanitized(rows)}
  except RemnawaveApiError as e:raise HTTPException(502,'Remnawave: '+str(e))from e
 @router.post('/api/web/admin/remnawave/{pid}/command')
 async def write(pid:int,p:PanelCommand,freeman_session:str|None=Cookie(default=None)):
  actor=await allowed(freeman_session);method,path,body=command(p)
  stamp=f'web:remnawave:{pid}:{p.request_id}';fingerprint=hashlib.sha256(json.dumps(p.model_dump(),sort_keys=True,separators=(',',':')).encode()).hexdigest()
  async with database.sessions.begin() as s:
   v=await panel(s,pid);repo=SQLRepository(s)
   await lock_nonce(s,stamp)
   old=await repo.setting(stamp)
   if old:
    old=json.loads(old)
    if old['fingerprint']!=fingerprint:raise HTTPException(409,'Идентификатор относится к другому действию')
    if old['state']=='done':return old['result']
    raise HTTPException(409,'Действие уже отправлено. Обновите данные панели перед повторной попыткой.')
   if p.resource=='panel':
    v.name=body['name'];result={'ok':True,'request_id':p.request_id}
    await repo.set_setting(stamp,json.dumps(dict(fingerprint=fingerprint,state='done',result=result)))
    s.add(AdminAudit(actor_telegram_id=actor.telegram_id or -actor.id,action='web_remnawave_panel_name',target=str(pid),details='name'))
    return result
   api=RemnawaveService(repo,settings).api(v)
   await repo.set_setting(stamp,json.dumps(dict(fingerprint=fingerprint,state='pending')))
  # The pending marker is committed BEFORE the remote call. No automatic retries.
  try:
   result=unwrap(await api.request(method,path,body))
   if p.action=='save':
    target=p.target or(result.get('uuid')if isinstance(result,dict)else None)
    if not target:raise RemnawaveApiError('Не подтверждён UUID сохранённой записи')
    current=unwrap(await api.request('GET','/'+p.resource+'/'+valid_uuid(target)))
    if not isinstance(current,dict)or any(k in p.data and current.get(k)!=p.data[k]for k in ('name','remark','address','port','isDisabled')):raise RemnawaveApiError('Изменение не подтверждено чтением панели')
  except (RemnawaveApiError,TimeoutError,asyncio.TimeoutError)as e:
   async with database.sessions.begin() as s:
    await SQLRepository(s).set_setting(stamp,json.dumps(dict(fingerprint=fingerprint,state='uncertain')))
    s.add(AdminAudit(actor_telegram_id=actor.telegram_id or -actor.id,action='web_remnawave_uncertain',target=str(pid),details=p.resource+':'+p.action))
   raise HTTPException(502,'Панель не подтвердила результат. Обновите её состояние. Действие автоматически не повторяется.')from e
  result={'ok':True,'request_id':p.request_id}
  async with database.sessions.begin() as s:
   await SQLRepository(s).set_setting(stamp,json.dumps(dict(fingerprint=fingerprint,state='done',result=result)))
   s.add(AdminAudit(actor_telegram_id=actor.telegram_id or -actor.id,action='web_remnawave_'+p.action,target=str(pid)+':'+p.target,details=p.resource+' fields:'+','.join(sorted(p.data))))
  return result
 return router
