"""Owner-confirmed, versioned service facts shared by all knowledge consumers."""
import hashlib,json,re,unicodedata
from sqlalchemy import select
from app.database.models import AppSetting,AIKnowledge,AIEvent,AIConversation,now
from app.database.repositories import SQLRepository
from app.services.customer_identity import lock_nonce
from app.services.freeman_ai.security import redact
STATES={'maintenance':'Технические работы','outage':'Недоступен','degraded':'Работает с перебоями','operational':'Работает'}
DEFS={
 'read_service_status':('Текущие подтверждённые состояния сервисов. Найди точный entity_id перед обновлением; при неоднозначности уточни у владельца.',{}),
 'set_service_status':('Предложить владельцу обновление состояния сервиса. Ничего не меняет до нажатия подтверждения. Повторно используй entity_id существующего сервиса; пустой только для нового. Только факты владельца, без выдуманных сроков.',{'entity_id':{'type':'string','maxLength':24},'entity':{'type':'string','maxLength':160},'status':{'type':'string','enum':list(STATES)},'message':{'type':'string','maxLength':1500}})
}
INSTRUCTIONS='''\nОперативная память: сообщения владельца о сбое, технических работах, ограничениях и восстановлении сервисов обрабатывай через read_service_status и set_service_status. Это отдельные актуальные состояния, не новые постоянные инструкции. Перед изменением прочитай состояния и используй точный entity_id; «починил», «восстановил», «снова работает» закрывает прежнюю неисправность того же сервиса статусом operational. Не создавай вторую запись для того же сервиса. Если «он», «тот сервер», страна или название подходят нескольким объектам — уточни. Вопрос, цитата клиента, предположение или гипотетический пример не являются сообщением о фактическом сбое: ничего не предлагай менять без уточнения. Не придумывай причину, время восстановления и факт проверки. Предложение требует кнопки подтверждения; не говори «сохранил», пока сервер не подтвердил. Для постоянных инструкций используй существующую базу знаний.'''

def explicit_knowledge(text):
 raw=text.casefold()
 return [{'type':'knowledge_write_authorized'}] if any(x in raw for x in ('добав','сохран','обнов','запиш','внес','измени','создай')) and any(x in raw for x in ('баз','инструкц')) else []

def entity_key(label):
 value=re.sub(r'\s+',' ',unicodedata.normalize('NFKC',label).casefold().replace('ё','е')).strip()
 return hashlib.sha256(value.encode()).hexdigest()[:24]

async def current(s):
 rows=await s.scalars(select(AppSetting).where(AppSetting.key.startswith('service_status_current:')).order_by(AppSetting.key).limit(200))
 return [json.loads(r.value) for r in rows]

async def prepare(s,settings,c,name,args):
 from app.services.freeman_ai.owner import authorize
 await authorize(s,settings,c)
 if name=='read_service_status':return {'items':await current(s),'limit':200}
 if name!='set_service_status' or set(args)!=set(DEFS[name][1]):raise ValueError('Неверные параметры состояния')
 if any(not isinstance(v,str) for v in args.values()):raise ValueError('Ожидается текст')
 label=redact(args['entity']).strip();body=redact(args['message']).strip();status=args['status'];eid=args['entity_id']
 if not label or len(label)>160 or not body or len(body)>1500 or status not in STATES:raise ValueError('Уточните сервис, статус и краткое сообщение')
 row=await s.get(AppSetting,'service_status_current:'+eid) if eid else None
 if eid and not row:raise ValueError('Сервис не найден. Прочитайте read_service_status и уточните объект.')
 if not eid:
  eid=entity_key(label);row=await s.get(AppSetting,'service_status_current:'+eid)
 previous=json.loads(row.value) if row else None
 if previous:label=previous['entity']
 payload=dict(entity_id=eid,entity=label,status=status,message=body,expected_revision=previous['revision'] if previous else 0)
 return {'saved':False,'requires_owner_confirmation':True,'actions':[dict(type='knowledge_proposal',title='Состояние: '+label,text=STATES[status]+'\n\n'+body+'\n\nПосле подтверждения прежнее состояние будет заменено; история сохранится.',service_status=payload)]}

async def confirm(s,settings,u,m,index,action):
 c=await s.get(AIConversation,m.conversation_id)
 if not c or c.channel!='owner' or c.user_id!=u.id or not settings.is_owner(u.telegram_id) or m.role!='assistant':raise PermissionError('Owner required')
 await lock_nonce(s,'service-status-write')
 repo=SQLRepository(s);done='service_status_done:'+str(m.id)+':'+str(index)
 saved=await repo.setting(done)
 if saved:return json.loads(saved)
 a=action['service_status'];eid=a['entity_id'];key='service_status_current:'+eid
 row=await s.get(AppSetting,key,populate_existing=True);previous=json.loads(row.value) if row else None
 revision=previous['revision'] if previous else 0
 if revision!=a['expected_revision']:raise ValueError('Состояние уже изменилось. Попросите ассистента обновить предложение.')
 if previous:await repo.set_setting('service_status_history:'+eid+':'+str(revision),json.dumps(previous,ensure_ascii=False))
 article=await s.get(AIKnowledge,previous['article_id']) if previous else None
 if not article:article=AIKnowledge(title='[Состояние сервиса] '+a['entity'],text='',enabled=True);s.add(article);await s.flush()
 article.text='Актуальное состояние: '+STATES[a['status']]+'.\n'+a['message']+'\nОбновлено владельцем: '+now().isoformat()+'.\nЭта запись заменяет прежние сведения о состоянии этого сервиса.'
 article.enabled=True;article.version=revision+1;article.updated_at=now()
 value={k:a[k] for k in ('entity_id','entity','status','message')};value.update(revision=revision+1,article_id=article.id,updated_at=now().isoformat())
 await repo.set_setting(key,json.dumps(value,ensure_ascii=False))
 result=dict(completed=True,saved=True,entity_id=eid,revision=revision+1,status=a['status'])
 await repo.set_setting(done,json.dumps(result))
 s.add(AIEvent(conversation_id=c.id,kind='service_status.updated',name=eid,details=json.dumps({'revision':revision+1,'message_id':m.id,'status':a['status']})))
 return result

def install(owner):
 owner.DEFS.update(DEFS);owner.INSTRUCTIONS+=INSTRUCTIONS
 original_execute=owner.execute;original_confirm=owner.confirm
 async def execute(s,settings,c,name,args):
  if name in DEFS:return await prepare(s,settings,c,name,args)
  return await original_execute(s,settings,c,name,args)
 async def confirm_action(s,settings,u,message_id,index):
  from app.database.models import AIMessage
  m=await s.get(AIMessage,message_id)
  actions=json.loads(m.actions) if m else []
  if m and 0<=index<len(actions) and actions[index].get('service_status'):
   return await confirm(s,settings,u,m,index,actions[index])
  return await original_confirm(s,settings,u,message_id,index)
 owner.execute=execute;owner.confirm=confirm_action
