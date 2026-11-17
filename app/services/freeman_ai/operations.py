"""Owner operations use the website's own validated handlers and transaction.
No arbitrary URL, SQL, shell, synthetic user, or external HTTP is accepted.
"""
import hashlib,json,re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlencode
from fastapi import FastAPI,APIRouter,HTTPException
from pydantic import BaseModel,Field
from typing import Literal
from fastapi.routing import APIRoute
from sqlalchemy import select
from app.database.models import AIMessage,AIEvent,AdminAudit
from app.database.repositories import SQLRepository
from .security import redact

DEFS={
 'chat_attachments':('Показать фотографии из текущей переписки: ID для прикрепления к публикации. Для импорта используй POST /api/web/admin/publication-images {image_id}, затем media/cover публикации из name результата. Не копируй фото без поручения владельца.',{}),
 'site_capabilities':('Найти существующие операции сайта/бота и их точные параметры. query: catalog, publications, keys, users, customers, proxy, offers, payments, support либо точный шаблон пути; пустой query показывает список разделов. Прочти схему перед подготовкой изменения. Возвращает только зарегистрированные административные операции.',{'query':{'type':'string','maxLength':180}}),
 'site_read':('Прочитать реальные данные через административный обработчик сайта. path конкретный путь из site_capabilities; query и payload — JSON-объекты строкой (пустой объект {}). Метод GET либо POST только для поиска/списка. Секретные поля исключаются.',{'method':{'type':'string','enum':['GET','POST']},'path':{'type':'string','maxLength':200},'query':{'type':'string','maxLength':3000},'payload':{'type':'string','maxLength':6000}}),
 'prepare_site_change':('Подготовить исполнение поручения владельца через сайт и бот. Сначала уточни недостающие параметры и прочти актуальные версии объектов. operations — JSON-массив до 8 действий {method,path,query,payload}. Результат шага можно подставить строкой $result.0.id или $result.0.version. Для создания/публикации поста: POST publications с content.ops (Quill), затем POST publications/$result.0.id/action с action=publish,version=$result.0.version. Не добавляй рассылку без поручения: notify_clients=false по умолчанию. Сайт проверит параметры, права, версии и лимиты; владелец запускает показанное действие одной кнопкой. Не утверждай, что предложение уже выполнено.',{'summary':{'type':'string','maxLength':500},'operations':{'type':'string','maxLength':24000}}),
 'site_change_status':('Проверить сохранённый результат выполнения предложения по номеру сообщения и индексу действия. Не называй выполненным без completed=true.',{'message_id':{'type':'string','maxLength':20},'index':{'type':'string','maxLength':10}}),
}
DEFS['execute_site_change']=('Выполнить прямое поручение владельца сейчас. Параметры как prepare_site_change; заранее прочти актуальные данные/версии. Только конкретное поручение последнего сообщения, не предложение из истории. При неопределённости спроси. Возвращает подтверждённый сервером результат.',DEFS['prepare_site_change'][1])
DEFS['service_facts']=('Быстро получить реальные сводные данные: catalog — актуальные тарифы, цены в копейках, акции и их версии; customers — количество клиентов, с почтой, подтверждённой почтой и Telegram. Без списка персональных данных.',{'section':{'type':'string','enum':['catalog','customers']}})
DEFS['customer_count']=('Точный подсчёт клиентов по любому сочетанию CRM-фильтров. filters — JSON-объект: status all/active/inactive/blocked/no_services, key_state all/active/none/soon7/expired, contact all/email/telegram/whatsapp/none, origin all/internal/external, proxy all/active/none/has/expired, activity all/today/recent7/idle30plus, q строка. Пустой объект — все клиенты. Используется общий серверный фильтр.',{'filters':{'type':'string','maxLength':3000}})
class ImageImport(BaseModel):
 image_id:int=Field(ge=1)

class BotOperation(BaseModel):
 action:str=Field(pattern=r'^admin:[a-zA-Z0-9_:\-]+$',max_length=200)
 mode:Literal['render','input']='render'
 value:str=Field(default='',max_length=6000)

def bot_router(s,settings,u):
 router=APIRouter()
 def present(screen):return clean({'text':screen.text,'buttons':[[{'label':label,'action':action} for label,action in row] for row in screen.rows],'has_photo':bool(screen.photo),'has_document':bool(screen.document)})
 @router.post('/api/web/admin/publication-images')
 async def import_image(p:ImageImport):
  from app.database.models import AIOwnerImage,AIConversation
  image=await s.scalar(select(AIOwnerImage).join(AIMessage,AIMessage.id==AIOwnerImage.message_id).join(AIConversation,AIConversation.id==AIMessage.conversation_id).where(AIOwnerImage.id==p.image_id,AIConversation.channel=='owner',AIConversation.user_id==u.id))
  if not image:raise HTTPException(404,'Фото недоступно')
  from .security import decrypt
  from app.web.posts import posts_router,MediaPayload
  raw=decrypt(settings,image.encrypted_data);header,data=raw.split(',',1);mime=header.split(':',1)[1].split(';',1)[0]
  async def actor(_):return u
  upload=next(r.endpoint for r in posts_router(SimpleNamespace(sessions=SessionAccess(s)),settings,actor).routes if r.path=='/api/web/admin/posts/media')
  return await upload(MediaPayload(name='assistant-photo',mime=mime,data=data),freeman_session=None)
 @router.get('/api/web/admin/bot/panel')
 async def panel(action:str='admin:home'):
  if action not in {'admin:home','admin:servers','admin:tariffs','admin:trial','admin:referrals','admin:promos','admin:buttons','admin:texts','admin:audit','admin:settings'}:raise HTTPException(422,'Используй POST bot/action для действия из меню')
  from app.services.admin import AdminService
  return present(await AdminService(SQLRepository(s),settings).render(action,u))
 @router.post('/api/web/admin/bot/action')
 async def act(p:BotOperation):
  from app.services.admin import AdminService
  service=AdminService(SQLRepository(s),settings)
  try:
   if p.mode=='render':screen=await service.render(p.action,u)
   elif p.action.startswith('admin:p:'):
    from app.services.proxies import ProxyService
    screen=await ProxyService(SQLRepository(s),settings).input(p.action,u,p.value)
   elif p.action.startswith('admin:v:'):
    from app.services.vpn import VpnService
    screen=await VpnService(SQLRepository(s),settings).input(p.action,u,p.value)
   elif p.action.startswith('admin:r:'):
    from app.services.remnawave import RemnawaveService
    screen=await RemnawaveService(SQLRepository(s),settings).input(p.action,u,p.value)
   elif p.action.startswith('admin:x:'):
    from app.services.control import ControlService
    screen=await ControlService(SQLRepository(s),settings).input(p.action,u,p.value)
   else:screen=await service.apply_input(p.action,p.value,u)
  except (ValueError,PermissionError) as e:raise HTTPException(409,str(e))
  return present(screen)
 return router

READ_POST={'/api/web/admin/users/search','/api/web/admin/keys/search','/api/web/admin/proxy/control/list','/api/web/admin/offers/preview','/api/web/admin/offers/bulk/preview','/api/web/admin/publications/preview'}
SENSITIVE=re.compile(r'(password|secret|token|authorization|private_key|api_key|encrypted|cipher|connection_uri|subscription_url|subscription_link)',re.I)
def clean(x):
 if isinstance(x,dict):return {k:clean(v) for k,v in x.items() if not SENSITIVE.search(k)}
 if isinstance(x,list):return [clean(v) for v in x[:100]]
 return redact(x) if isinstance(x,str) else x

class SessionAccess:
 def __init__(self,s):self.s=s
 @asynccontextmanager
 async def context(self):yield self.s
 def __call__(self):return self.context()
 def begin(self):return self.context()

def app_for(s,settings,u):
 database=SimpleNamespace(sessions=SessionAccess(s))
 async def actor(_):return u
 app=FastAPI()
 app.router.routes.extend(bot_router(s,settings,u).routes)
 from app.web.admin_dashboard import admin_dashboard_router
 from app.web.catalog import catalog_router
 from app.web.customer_profile import customer_profile_router
 from app.web.offers import offers_router
 from app.web.admin_reports import admin_reports_router
 from app.web.admin_payments import admin_payments_router
 from app.web.admin_proxy import admin_proxy_router
 from app.web.proxy_control import proxy_control_router
 from app.web.posts import posts_router
 for factory in (admin_dashboard_router,catalog_router,customer_profile_router,offers_router,admin_reports_router,admin_payments_router,admin_proxy_router,proxy_control_router,posts_router):app.router.routes.extend(factory(database,settings,actor).routes)
 # Reuse cabinet support/content handlers and their account authorization.
 from app.web.routes import website_router
 for r in website_router(database,settings).routes:
  if isinstance(r,APIRoute) and (r.path.startswith('/api/web/admin/support/') or r.path in ('/api/web/admin/content','/api/web/admin/links','/api/web/admin/links/{item_id}','/api/web/admin/news','/api/web/admin/news/{item_id}') or re.fullmatch(r'/api/web/support/tickets/\{ticket_id\}(?:/(?:close|status|flags))?',r.path)):
   app.router.routes.append(r)
 return app

def allowed(r):
 return isinstance(r,APIRoute) and (r.path.startswith('/api/web/admin/') or r.path=='/api/web/posts/{id}' or r.path.startswith('/api/web/support/tickets/')) and not any(x in r.path for x in ('/avatar','/export','/media','/test'))

def route_for(app,method,path):
 if '?' in path or '..' in path or not path.startswith('/api/web/') or len(path)>200:raise ValueError('Invalid operation path')
 for r in app.routes:
  if allowed(r) and method in r.methods and r.path_regex.fullmatch(path):return r
 raise ValueError('Unknown or unsupported site operation')

def obj(raw):
 x=json.loads(raw or '{}')
 if not isinstance(x,dict):raise ValueError('Expected JSON object')
 return x

def steps(raw):
 x=json.loads(raw)
 if not isinstance(x,list) or not 1<=len(x)<=8:raise ValueError('Use 1–8 operations')
 for op in x:
  if not isinstance(op,dict) or set(op)-{'method','path','query','payload'}:raise ValueError('Invalid operation')
  if op.get('method') not in ('POST','PUT','PATCH','DELETE') or not isinstance(op.get('path'),str):raise ValueError('Invalid method/path')
  for field in ('query','payload'):
   if not isinstance(op.get(field,{}),dict):raise ValueError('Expected object')
 return x

def resolve(x,results):
 if isinstance(x,str):
  def value(m):
   try:
    v=results[int(m[1])]
    for key in m[2].split('.'):v=v[int(key)] if isinstance(v,list) else v[key]
    if isinstance(v,(dict,list)):raise ValueError()
    return v
   except (IndexError,KeyError,ValueError,TypeError):raise ValueError('Invalid previous-result reference')
  match=re.fullmatch(r'\$result\.(\d+)\.([\w.]+)',x)
  if match:return value(match)
  return re.sub(r'\$result\.(\d+)\.([\w.]+)',lambda m:str(value(m)),x)
 if isinstance(x,dict):return {k:resolve(v,results) for k,v in x.items()}
 if isinstance(x,list):return [resolve(v,results) for v in x]
 return x

async def invoke(app,s,settings,u,method,path,query,payload):
 route_for(app,method,path)
 from cryptography.fernet import Fernet
 import time
 token=Fernet(settings.encryption_key.encode()).encrypt(json.dumps(dict(user_id=u.id,version=u.auth_version,expires=int(time.time())+60)).encode()).decode()
 raw=json.dumps(payload).encode();sent=False;messages=[]
 async def receive():
  nonlocal sent
  if not sent:sent=True;return {'type':'http.request','body':raw,'more_body':False}
  return {'type':'http.disconnect'}
 async def send(message):messages.append(message)
 scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,'scheme':'https','path':path,'raw_path':path.encode(),'root_path':'','query_string':urlencode(query,doseq=True).encode(),'headers':[(b'content-type',b'application/json'),(b'cookie',('freeman_session='+token).encode())],'server':('assistant.internal',443),'client':('127.0.0.1',0)}
 await app(scope,receive,send)
 status=next(m['status'] for m in messages if m['type']=='http.response.start');body=b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body')
 if len(body)>500000:raise ValueError('Use a narrower query')
 try:data=json.loads(body) if body else {'ok':True}
 except (ValueError,UnicodeDecodeError):raise ValueError('Operation returned a file, use website export')
 if status>=400:raise ValueError('Сайт отклонил операцию: '+str(clean(data.get('detail',data)))[:1500])
 return clean(data)

async def execute(s,settings,c,name,args):
 from .owner import authorize
 u=await authorize(s,settings,c)
 if name=='chat_attachments':
  from app.database.models import AIOwnerImage
  rows=await s.execute(select(AIOwnerImage.id,AIMessage.id,AIMessage.created_at).join(AIMessage,AIMessage.id==AIOwnerImage.message_id).where(AIMessage.conversation_id==c.id).order_by(AIOwnerImage.id.desc()).limit(20))
  return {'images':[{'image_id':row[0],'message_id':row[1],'created_at':row[2].isoformat()} for row in rows]}
 if name=='site_change_status':
  m=await s.get(AIMessage,int(args['message_id']));conv=await s.get(__import__('app.database.models',fromlist=['AIConversation']).AIConversation,m.conversation_id) if m else None
  if not conv or conv.user_id!=u.id or conv.channel!='owner':raise PermissionError('Unavailable proposal')
  from app.web.posts import load_json
  data=await load_json(SQLRepository(s),f'ai_site_done:{m.id}:{int(args["index"])}')
  return {'completed':bool(data),'result':data}
 if name=='service_facts':
  if args['section']=='catalog':
   data=await invoke(app_for(s,settings,u),s,settings,u,'GET','/api/web/admin/catalog',{},{});data.pop('history',None)
   return {'data':data,'money_unit':'kopecks','as_of':__import__('app.database.models',fromlist=['now']).now().isoformat()}
  from app.database.models import User
  from sqlalchemy import func
  count=lambda condition:select(func.count()).select_from(User).where(condition)
  return {'total':await s.scalar(select(func.count()).select_from(User)),'with_email':await s.scalar(count(User.email.is_not(None)&(func.trim(User.email)!=''))),'verified_email':await s.scalar(count(User.email_verified.is_(True)&User.email.is_not(None)&(func.trim(User.email)!=''))),'with_telegram':await s.scalar(count(User.telegram_id.is_not(None)))}
 if name=='customer_count':
  from app.web.admin_dashboard import AdminQuery
  from app.services.client_registry import query
  from sqlalchemy import func
  filters=obj(args['filters']);unknown=set(filters)-set(AdminQuery.model_fields)
  if unknown:raise ValueError('Неизвестные фильтры: '+','.join(sorted(unknown)))
  stmt=await query(s,settings,AdminQuery(**filters))
  return {'count':await s.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())),'filters':filters,'source':'Freeman ONE / CRM'}
 app=app_for(s,settings,u)
 if name=='site_capabilities':
  query=args['query'].strip().lower();routes=[r for r in app.routes if allowed(r)]
  if not query:return {'groups':sorted({r.path.split('/')[4] for r in routes if r.path.startswith('/api/web/admin/')}),'other':['support/tickets','posts/{id}'],'usage':'Укажи раздел или точный путь. Для изменений прочти схему и актуальные версии.'}
  if any(r.path.lower()==query for r in routes):routes=[r for r in routes if r.path.lower()==query]
  schema=app.openapi();components=schema.get('components',{}).get('schemas',{})
  found=[]
  for r in routes:
   if query not in r.path.lower():continue
   for method in sorted(r.methods):
    definition=schema['paths'][r.path][method.lower()]
    definition={k:v for k,v in definition.items() if k in ('summary','description','parameters','requestBody')}
    refs=set(re.findall(r'#/components/schemas/([\w]+)',json.dumps(definition)))
    definitions={}
    while refs:
     ref=refs.pop()
     if ref in definitions:continue
     definitions[ref]=components.get(ref,{})
     refs.update(set(re.findall(r'#/components/schemas/([\w]+)',json.dumps(definitions[ref])))-set(definitions))
    found.append(dict(method=method,path=r.path,read_only=method=='GET' or r.path in READ_POST,definition=definition,schemas=definitions))
  if len(json.dumps(found[:18],ensure_ascii=False).encode())>14000:return {'operations':[{k:o[k] for k in ('method','path','read_only')} for o in found],'total':len(found),'narrow_query':True,'instruction':'Укажи точный шаблон path для схемы параметров.'}
  return {'operations':found[:18],'total':len(found),'narrow_query':len(found)>18}
 if name=='site_read':
  r=route_for(app,args['method'],args['path'])
  if args['method']!='GET' and r.path not in READ_POST:raise PermissionError('Use prepare_site_change for writes')
  data=await invoke(app,s,settings,u,args['method'],args['path'],obj(args['query']),obj(args['payload']))
  if len(json.dumps(data,ensure_ascii=False).encode())>20000:return {'source':'Freeman ONE','error':'Ответ слишком большой. Уменьши limit или уточни объект/фильтр.'}
  return {'source':'Freeman ONE / общая база сайта и бота','data':data}
 operations=steps(args['operations'])
 for op in operations:
  path=re.sub(r'\$result\.\d+\.[\w.]+','1',op['path']);route_for(app,op['method'],path)
 if not args['summary'].strip():raise ValueError('Describe proposed changes')
 action=dict(type='site_change',summary=args['summary'],operations=operations)
 if name=='execute_site_change':
  latest=await s.scalar(select(AIMessage).where(AIMessage.conversation_id==c.id,AIMessage.role=='client').order_by(AIMessage.id.desc()).limit(1))
  if not latest or not re.search(r'удал|созда|опублику|измен|выда|продл|сброс|отвяж|заблок|разблок|перевыпуст|попол|спиш|обнов|отправ|сдела|добав|выполн|примени',latest.text,re.I):raise ValueError('Нужно прямое поручение владельца на изменение. Пока используй prepare_site_change.')
  from .service import append
  key='execute:'+str(latest.id)
  m=await append(s,settings,c,'Операция сайта',key,'assistant',[action])
  result=await confirm(s,settings,u,m,0,action)
  rows=[]
  for i,r in enumerate(result['results']):
   status='Удалён из каталога; история сохранена' if r.get('deleted') and r.get('history_preserved') else r.get('status') or 'Подтверждено сайтом'
   rows.append([str(i+1),str(r.get('id','—')),str(status)])
  return {'executed':True,'result':result,'actions':[{'type':'rich_block','block':{'type':'table','title':'Подтверждение выполнения','columns':['Шаг','ID','Результат'],'rows':rows}}]}
 return {'completed':False,'requires_owner_confirmation':True,'actions':[action]}

async def confirm(s,settings,u,m,index,action):
 from app.services.customer_identity import lock_nonce
 await lock_nonce(s,f'ai:site:{m.id}:{index}')
 from app.web.posts import load_json,store_json
 repo=SQLRepository(s);key=f'ai_site_done:{m.id}:{index}';saved=await load_json(repo,key)
 if saved:return saved
 app=app_for(s,settings,u);results=[]
 async with s.begin_nested():
  for i,op in enumerate(steps(json.dumps(action['operations']))):
   step=resolve(op,results);path=step['path'];method=step['method'];payload=step.get('payload',{});route=route_for(app,method,path)
   body=route.body_field
   if body:
    fields=getattr(getattr(body,'type_',None) or body.field_info.annotation,'model_fields',{})
    if 'confirmed' in fields:payload['confirmed']=True
    if 'notify_clients' in fields:payload.setdefault('notify_clients',False)
    if 'request_id' in fields:payload['request_id']=hashlib.sha256(f'ai:{m.id}:{index}:{i}'.encode()).hexdigest()[:32]
   results.append(await invoke(app,s,settings,u,method,path,step.get('query',{}),payload))
  result={'completed':True,'summary':action['summary'],'results':results}
  await store_json(repo,key,result)
  s.add(AdminAudit(actor_telegram_id=u.telegram_id,action='assistant.site_change',target=str(m.id),details=json.dumps({'steps':[{'method':o['method'],'path':o['path']} for o in action['operations']]})))
  s.add(AIEvent(conversation_id=m.conversation_id,kind='owner.site_change',name=str(m.id),details=json.dumps({'actor_id':u.id,'steps':len(results)})))
 return result
