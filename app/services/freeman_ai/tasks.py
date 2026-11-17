"""Durable one-shot owner reports; execution begins at due_at, output stays in owner chat."""
import hashlib,json
from datetime import datetime,timedelta
from sqlalchemy import select,func
from app.database.models import AIOwnerTask,AIJob,AIMessage,AIConversation,User,now
from app.services.customer_identity import lock_nonce
from .security import aware,redact

DEFS={
 'schedule_report':('По явной просьбе владельца поставить одноразовую задачу анализа/отчёта. Начало выполнения в due_at, результат в личном AI-чате. Не обещает другие действия.',{'prompt':{'type':'string','maxLength':2000},'due_at':{'type':'string','maxLength':50}}),
 'schedule_reminder':('Создать напоминание владельцу по его просьбе. Текст появится в личном чате в due_at (ISO с часовым поясом).',{'prompt':{'type':'string','maxLength':2000},'due_at':{'type':'string','maxLength':50}}),
 'list_tasks':('Показать последние 20 задач владельца и реальные статусы.',{}),
 'cancel_task':('Отменить задачу владельца по его явному запросу.',{'task_id':{'type':'string','maxLength':20}}),
}
def task_data(t):
 try:return json.loads(t.prompt) if t.prompt.startswith('{') else {}
 except ValueError:return {}
def present(t):return dict(id=t.id,kind=task_data(t).get('kind','report'),conversation_id=t.conversation_id,prompt=task_data(t).get('title',t.prompt),due_at=aware(t.due_at).isoformat(),status=t.status,finished_at=aware(t.finished_at).isoformat() if t.finished_at else None)
async def execute(s,settings,c,name,args):
 from .owner import authorize
 u=await authorize(s,settings,c)
 if name=='list_tasks':return {'tasks':[present(t) for t in await s.scalars(select(AIOwnerTask).where(AIOwnerTask.owner_id==u.id).order_by(AIOwnerTask.id.desc()).limit(20))]}
 if name=='cancel_task':
  if not args['task_id'].isdigit():raise ValueError('Invalid task')
  t=await s.get(AIOwnerTask,int(args['task_id']),with_for_update=True)
  if not t or t.owner_id!=u.id:raise PermissionError('Task unavailable')
  if t.status not in ('scheduled','running'):return present(t)
  t.status='canceled';t.finished_at=now()
  if t.job_id:
   job=await s.get(AIJob,t.job_id,with_for_update=True)
   if job and job.status in ('pending','processing'):job.status='canceled'
  return present(t)
 try:due=datetime.fromisoformat(args['due_at'])
 except (ValueError,TypeError):raise ValueError('Use ISO datetime with timezone')
 if due.tzinfo is None or due<=now() or due>now()+timedelta(days=90):raise ValueError('Нужно время с часовым поясом, в будущем, не позже 90 дней.')
 prompt=redact(args['prompt']).strip()
 if name=='schedule_reminder':prompt=json.dumps({'kind':'reminder','title':prompt},ensure_ascii=False)
 if not prompt:raise ValueError('Empty task')
 await lock_nonce(s,'ai:owner-tasks:'+str(u.id))
 key=hashlib.sha256(f'{c.id}:{c.generation}:{prompt}:{due.isoformat()}'.encode()).hexdigest()
 t=await s.scalar(select(AIOwnerTask).where(AIOwnerTask.request_key==key))
 if not t:
  count=await s.scalar(select(func.count()).select_from(AIOwnerTask).where(AIOwnerTask.owner_id==u.id,AIOwnerTask.status.in_(('scheduled','running'))))
  if count>=20:raise ValueError('Не более 20 незавершённых задач.')
  t=AIOwnerTask(owner_id=u.id,conversation_id=c.id,request_key=key,prompt=prompt,due_at=due);s.add(t);await s.flush()
 return {'task':present(t),'execution':'Напоминание появится в личном чате в указанное время.' if name=='schedule_reminder' else 'Выполнение начнётся в указанное время; результат появится в личном AI-чате.'}
async def tick(database,settings):
 from .service import enabled,append
 async with database.sessions.begin() as s:
  if not await enabled(s,settings):return
  rows=list(await s.scalars(select(AIOwnerTask).where(AIOwnerTask.status=='running').with_for_update(skip_locked=True).limit(20)))
  for t in rows:
   job=await s.get(AIJob,t.job_id)
   if job and job.status in ('done','failed','superseded','canceled'):
    t.status='done' if job.status=='done' else 'failed' if job.status!='canceled' else 'canceled';t.finished_at=now()
  rows=list(await s.scalars(select(AIOwnerTask).where(AIOwnerTask.status=='scheduled',AIOwnerTask.due_at<=now()).order_by(AIOwnerTask.due_at).with_for_update(skip_locked=True).limit(10)))
  for t in rows:
   u=await s.get(User,t.owner_id);c=await s.get(AIConversation,t.conversation_id)
   if not u or u.status!='active' or not settings.is_owner(u.telegram_id) or not c or c.channel!='owner' or c.mode!='ai':
    t.status='failed';t.finished_at=now();continue
   data=task_data(t)
   if data.get('kind')=='reminder':
    await append(s,settings,c,'Напоминание: '+data['title'],'task-reminder:'+str(t.id),'system');t.status='done';t.finished_at=now();continue
   if data.get('kind')=='operation':
    try:
     async with s.begin_nested():
      from .owner import confirm
      await confirm(s,settings,u,data['message_id'],data['index'])
     await append(s,settings,c,'Запланированная задача выполнена: '+data['title'],'task-operation:'+str(t.id),'system');t.status='done'
    except Exception as error:
     await append(s,settings,c,'Не удалось выполнить задачу: '+(redact(str(error))[:1200] if isinstance(error,(ValueError,PermissionError)) else 'Сервис временно недоступен. Откройте беседу и проверьте результат перед повтором.'),'task-operation:'+str(t.id),'system');t.status='failed'
    t.finished_at=now();continue
   await lock_nonce(s,'ai:scheduled-conversation:'+str(c.id))
   busy=await s.scalar(select(AIJob.id).join(AIMessage,AIMessage.id==AIJob.message_id).where(AIMessage.conversation_id==c.id,AIJob.status.in_(('pending','processing'))).limit(1))
   if busy:continue
   m=await append(s,settings,c,f'Запланированный отчёт #{t.id}. '+t.prompt,'task:'+str(t.id),actions=[{'type':'scheduled_report','task_id':t.id}])
   await s.flush();job=await s.scalar(select(AIJob).where(AIJob.message_id==m.id));t.job_id=job.id;t.status='running'

async def schedule_operation(s,settings,u,message_id,index,due_at):
 from app.database.models import AIMessage,AIConversation
 m=await s.get(AIMessage,message_id);c=await s.get(AIConversation,m.conversation_id) if m else None
 if not c or c.user_id!=u.id or c.channel!='owner' or c.mode!='ai':raise PermissionError('Чат недоступен')
 actions=json.loads(m.actions)
 if index>=len(actions) or actions[index].get('type')!='site_change':raise ValueError('Действие недоступно')
 title=actions[index]['summary']
 result=await execute(s,settings,c,'schedule_report',{'prompt':json.dumps({'kind':'operation','title':title,'message_id':message_id,'index':index},ensure_ascii=False),'due_at':due_at})
 t=await s.get(AIOwnerTask,result['task']['id'])
 t.prompt=json.dumps({'kind':'operation','title':title,'message_id':message_id,'index':index},ensure_ascii=False)
 return present(t)
