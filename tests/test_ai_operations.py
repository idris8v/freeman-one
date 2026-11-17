"""Real admin handlers with isolated DB, fake model; never sends to customers."""
import json,unittest
from sqlalchemy import select,func
from test_ai_owner import OwnerTests
from app.database.models import User,AIMessage,Publication,WebNews,Tariff,Campaign,now
from app.services.freeman_ai import owner,service,operations
class OperationTests(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=OwnerTests.asyncSetUp
 asyncTearDown=OwnerTests.asyncTearDown
 async def context(self,s):
  self.settings.is_admin=lambda tid:tid==123
  return await owner.conversation(s,self.settings,await s.get(User,self.uid))
 async def test_capabilities_and_read(self):
  async with self.db.sessions.begin() as s:
   c=await self.context(s)
   result=await owner.execute(s,self.settings,c,'site_capabilities',{'query':'catalog'})
   self.assertTrue(any(o['method']=='POST' and 'Change' in o['schemas'] for o in result['operations']))
   result=await owner.execute(s,self.settings,c,'site_read',dict(method='GET',path='/api/web/admin/catalog',query='{}',payload='{}'))
   self.assertIn('vpn',result['data'])
   with self.assertRaises(PermissionError):await owner.execute(s,self.settings,c,'site_read',dict(method='POST',path='/api/web/admin/catalog',query='{}',payload='{}'))
 async def test_publish_chain_idempotency_and_status(self):
  async with self.db.sessions.begin() as s:
   c=await self.context(s);ops=[dict(method='POST',path='/api/web/admin/publications',payload=dict(title='Тестовая инструкция',content={'ops':[{'insert':'Шаг 1. Открой приложение.\n'}]})),dict(method='POST',path='/api/web/admin/publications/$result.0.id/action',payload=dict(action='publish',version='$result.0.version'))]
   result=await owner.execute(s,self.settings,c,'prepare_site_change',dict(summary='Опубликовать инструкцию без рассылки',operations=json.dumps(ops)))
   self.assertFalse(result['completed']);self.assertEqual(await s.scalar(select(func.count()).select_from(Publication)),0)
   m=await service.append(s,self.settings,c,'Публикация подготовлена','operation-test','assistant',result['actions'])
   u=await s.get(User,self.uid);done=await owner.confirm(s,self.settings,u,m.id,0)
   self.assertTrue(done['completed']);self.assertEqual(done['results'][-1]['status'],'published')
   self.assertEqual(done,await owner.confirm(s,self.settings,u,m.id,0));self.assertEqual(await s.scalar(select(func.count()).select_from(Publication)),1)
   status=await owner.execute(s,self.settings,c,'site_change_status',dict(message_id=str(m.id),index='0'));self.assertTrue(status['completed'])
   self.assertEqual(await s.scalar(select(func.count()).select_from(Campaign)),0)
   with self.assertRaises(PermissionError):await owner.confirm(s,self.settings,await s.get(User,self.other),m.id,0)
 async def test_invalid_chain_rolls_back_and_customer_denied(self):
  async with self.db.sessions.begin() as s:
   c=await self.context(s);ops=[dict(method='POST',path='/api/web/admin/publications',payload=dict(title='Черновик',notify_clients=False)),dict(method='POST',path='/api/web/admin/publications/$result.0.id/action',payload=dict(action='publish',version=99))]
   result=await owner.execute(s,self.settings,c,'prepare_site_change',dict(summary='Проверка конфликта версии',operations=json.dumps(ops)))
   m=await service.append(s,self.settings,c,'Изменения','rollback-test','assistant',result['actions'])
   with self.assertRaises(ValueError):await owner.confirm(s,self.settings,await s.get(User,self.uid),m.id,0)
   self.assertEqual(await s.scalar(select(func.count()).select_from(Publication)),0)
   with self.assertRaises(ValueError):await owner.execute(s,self.settings,c,'prepare_site_change',dict(summary='Bad',operations=json.dumps([dict(method='POST',path='/api/web/admin/assistant/config')])) )
   customer=await service.conversation(s,self.settings,user_id=self.other)
   with self.assertRaises(PermissionError):await owner.execute(s,self.settings,customer,'site_capabilities',{'query':''})
 def test_reference_and_secret_scrubbing(self):
  self.assertEqual(operations.resolve('/api/web/admin/publications/$result.0.id/action',[{'id':12}]),'/api/web/admin/publications/12/action')
  self.assertEqual(operations.clean({'token':'SECRET','nested':{'api_key':'SECRET','id':1}}),{'nested':{'id':1}})
 async def test_catalog_promotion_and_exact_interval(self):
  from datetime import timedelta
  from app.database.models import Payment
  async with self.db.sessions.begin() as s:
   c=await self.context(s)
   change=dict(kind='promotion',name='Тестовая акция',value='15',discount_kind='percent',product='vpn',target='renewal')
   proposal=await owner.execute(s,self.settings,c,'prepare_site_change',dict(summary='Скидка 15% на продление VPN',operations=json.dumps([dict(method='POST',path='/api/web/admin/catalog',payload=change)])))
   m=await service.append(s,self.settings,c,'Акция','promo-test','assistant',proposal['actions']);result=await owner.confirm(s,self.settings,await s.get(User,self.uid),m.id,0)
   self.assertTrue(result['completed'])
   catalog=await owner.execute(s,self.settings,c,'site_read',dict(method='GET',path='/api/web/admin/catalog',query='{}',payload='{}'))
   self.assertTrue(any(p['name']=='Тестовая акция' for p in catalog['data']['promotions']))
   if s.bind.dialect.name!='postgresql':return
   hi=now();s.add_all([Payment(user_id=self.other,amount=10000,status='manual_paid',created_at=hi-timedelta(hours=1)),Payment(user_id=self.other,amount=90000,status='manual_paid',created_at=hi-timedelta(hours=4))]);await s.flush()
   report=await owner.execute(s,self.settings,c,'sales_report',dict(period='interval',start=(hi-timedelta(hours=3)).isoformat(),end=hi.isoformat(),kind='all'))
   self.assertEqual(report['report']['stats']['revenue'],10000);self.assertEqual(report['report']['stats']['sales'],1)
   self.assertIn('T',report['report']['start'])

 async def test_bot_control_reuses_same_settings(self):
  from app.database.repositories import SQLRepository
  from app.config.admin_content import EDITABLE_BUTTONS
  async with self.db.sessions.begin() as s:
   c=await self.context(s);target=next(iter(EDITABLE_BUTTONS))
   proposal=await owner.execute(s,self.settings,c,'prepare_site_change',dict(summary='Изменить подпись кнопки бота',operations=json.dumps([dict(method='POST',path='/api/web/admin/bot/action',payload=dict(mode='input',action='admin:input:button:'+target,value='Тестовая подпись'))])))
   m=await service.append(s,self.settings,c,'Кнопка','bot-test','assistant',proposal['actions'])
   await owner.confirm(s,self.settings,await s.get(User,self.uid),m.id,0)
   self.assertEqual(await SQLRepository(s).setting('button:'+target),'Тестовая подпись')
 async def test_photo_import_uses_owned_attachment(self):
  import base64,io,tempfile
  from pathlib import Path
  from unittest.mock import patch
  from PIL import Image
  from app.services.freeman_ai.images import store
  async with self.db.sessions.begin() as s:
   c=await self.context(s);m=await service.append(s,self.settings,c,'Опубликуй это фото','photo-import-test')
   out=io.BytesIO();Image.new('RGB',(2,2),'white').save(out,format='PNG')
   await store(s,self.settings,m.id,['data:image/png;base64,'+base64.b64encode(out.getvalue()).decode()]);await s.flush()
   refs=await owner.execute(s,self.settings,c,'chat_attachments',{});image_id=refs['images'][0]['image_id']
   ops=[dict(method='POST',path='/api/web/admin/publication-images',payload={'image_id':image_id}),dict(method='POST',path='/api/web/admin/publications',payload={'title':'Фото','content':{'ops':[{'insert':'Описание\n'}]},'media':[{'name':'$result.0.name','kind':'image'}]})]
   proposed=await owner.execute(s,self.settings,c,'prepare_site_change',dict(summary='Пост с фотографией',operations=json.dumps(ops)))
   a=await service.append(s,self.settings,c,'Подготовлено','photo-proposal','assistant',proposed['actions'])
   with tempfile.TemporaryDirectory() as tmp,patch('app.web.posts.STORAGE',Path(tmp)):
    done=await owner.confirm(s,self.settings,await s.get(User,self.uid),a.id,0)
    name=done['results'][0]['name'];self.assertTrue((Path(tmp)/name).exists());self.assertEqual(done['results'][1]['media'][0]['name'],name)
 async def test_reminder_and_scheduled_change(self):
  from datetime import timedelta
  from app.services.freeman_ai import tasks
  from app.database.models import AIOwnerTask
  async with self.db.sessions.begin() as s:
   c=await self.context(s)
   reminder=await tasks.execute(s,self.settings,c,'schedule_reminder',{'prompt':'Проверить продажи','due_at':(now()+timedelta(hours=1)).isoformat()})
   t=await s.get(AIOwnerTask,reminder['task']['id']);self.assertEqual(tasks.present(t)['kind'],'reminder');t.due_at=now()-timedelta(seconds=1)
   proposal=await owner.execute(s,self.settings,c,'prepare_site_change',{'summary':'Создать черновик позже','operations':json.dumps([dict(method='POST',path='/api/web/admin/publications',payload={'title':'Отложенный черновик','notify_clients':False})])})
   m=await service.append(s,self.settings,c,'Готово','scheduled-test','assistant',proposal['actions'])
   result=await tasks.schedule_operation(s,self.settings,await s.get(User,self.uid),m.id,0,(now()+timedelta(hours=2)).isoformat())
   t=await s.get(AIOwnerTask,result['id']);t.due_at=now()-timedelta(seconds=1)
  await tasks.tick(self.db,self.settings)
  await tasks.tick(self.db,self.settings)
  async with self.db.sessions() as s:
   self.assertEqual(await s.scalar(select(func.count()).select_from(Publication)),1)
   self.assertTrue(all(t.status=='done' for t in await s.scalars(select(AIOwnerTask))))
 async def test_service_counts_and_direct_tariff_deletion(self):
  from app.database.repositories import SQLRepository
  async with self.db.sessions.begin() as s:
   c=await self.context(s);u=await s.get(User,self.uid);u.email='owner@example.test';u.email_verified=True
   counts=await owner.execute(s,self.settings,c,'service_facts',{'section':'customers'})
   self.assertGreaterEqual(counts['with_email'],1);self.assertGreaterEqual(counts['verified_email'],1)
   t=Tariff(months=8,price=80000,device_limit=2);s.add(t);await s.flush()
   await service.append(s,self.settings,c,'Удали тариф 8 месяцев','delete-request')
   args={'summary':'Удалить тариф из каталога','operations':json.dumps([{'method':'DELETE','path':f'/api/web/admin/catalog/vpn/{t.id}','query':{'version':0}}])}
   result=await owner.execute(s,self.settings,c,'execute_site_change',args)
   self.assertTrue(result['executed']);self.assertTrue(result['result']['results'][0]['deleted'])
   catalog=await owner.execute(s,self.settings,c,'service_facts',{'section':'catalog'})
   self.assertFalse(any(x['id']==t.id for x in catalog['data']['vpn']))
   self.assertIsNone(await s.get(Tariff,t.id));self.assertEqual(await SQLRepository(s).setting(f'tariff_enabled:id{t.id}'),'0')
   self.assertEqual(result,await owner.execute(s,self.settings,c,'execute_site_change',args))
 async def test_unverified_success_is_not_displayed(self):
  from app.services.freeman_ai.engine import run
  class Liar:
   async def complete(self,*args):return {'usage':{'total_tokens':100},'output':[{'type':'message','content':[{'type':'output_text','text':'Я удалил тариф.'}]}]}
  async with self.db.sessions.begin() as s:
   c=await self.context(s);m=await service.append(s,self.settings,c,'Удали тариф','no-confirmation')
   text,actions,_=await run(s,self.settings,c,[m],Liar())
   self.assertIn('не подтверждено',text);self.assertNotIn('Я удалил',text)
 async def test_context_modes_keep_all_tools(self):
  from app.database.repositories import SQLRepository
  from app.services.freeman_ai.engine import run
  class Capture:
   def __init__(self):self.calls=[]
   async def complete(self,items,instructions,tools):
    self.calls.append((len(items),{x['name'] for x in tools}));return {'usage':{'total_tokens':100},'output':[{'type':'message','content':[{'type':'output_text','text':'Привет!'}]}]}
  async with self.db.sessions.begin() as s:
   c=await self.context(s);history=[]
   for i in range(15):history.append(await service.append(s,self.settings,c,'Справка '+str(i),'context:'+str(i),'assistant'))
   p=Capture();await run(s,self.settings,c,history,p)
   await SQLRepository(s).set_setting('assistant_context:'+str(c.id),'open');await run(s,self.settings,c,history,p)
   self.assertLess(p.calls[0][0],p.calls[1][0]);self.assertEqual(p.calls[0][1],p.calls[1][1]);self.assertIn('execute_site_change',p.calls[0][1])

 async def test_direct_execution_finishes_before_second_model_round(self):
  from app.services.freeman_ai.engine import run
  class Command:
   calls=0
   async def complete(self,*args):
    self.calls+=1
    if self.calls>1:raise AssertionError('Model called after confirmed write')
    return {'usage':{'total_tokens':100},'output':[{'type':'function_call','name':'execute_site_change','call_id':'direct-write','arguments':json.dumps({'summary':'Создать тариф','operations':json.dumps([{'method':'POST','path':'/api/web/admin/catalog','payload':{'kind':'vpn','name':'Тест 9 месяцев','months':9,'price':'10','devices':1}}])})}]}
  async with self.db.sessions.begin() as s:
   c=await self.context(s);m=await service.append(s,self.settings,c,'Создай тариф 9 месяцев, 1 устройство, 10 рублей','direct-create')
   provider=Command();text,actions,_=await run(s,self.settings,c,[m],provider)
   self.assertEqual(provider.calls,1);self.assertIn('сохранена',text)
   tariff=await s.scalar(select(Tariff).where(Tariff.months==9));self.assertEqual(tariff.price,1000)
   self.assertTrue(any(a.get('type')=='rich_block' for a in actions))
