"""Owner tools, durable reports and explicit attribution. All model/network calls mocked."""
import json,unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from sqlalchemy import select,func
from test_ai_owner import OwnerTests
from app.database.models import User,Payment,AIConversation,AIMessage,AIJob,AIOutbox,AIOwnerTask,AICheckoutLink,AIOrderAttribution,GuestOrder,AppSetting,now
from app.services.freeman_ai import owner,service,tasks,attribution
from app.services.freeman_ai.worker import process_one

class AssistantTests(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=OwnerTests.asyncSetUp
 asyncTearDown=OwnerTests.asyncTearDown
 async def oc(self,s):return await owner.conversation(s,self.settings,await s.get(User,self.uid))
 async def test_client_and_payment_read_tools(self):
  async with self.db.sessions.begin() as s:
   c=await self.oc(s);p=Payment(user_id=self.other,amount=15000,status='pending',provider_id='SECRET_PROVIDER');s.add(p);await s.flush()
   r=await owner.execute(s,self.settings,c,'find_client',{'query':'Bob'});self.assertEqual(r['clients'][0]['id'],self.other)
   r=await owner.execute(s,self.settings,c,'client_detail',{'client_id':str(self.other)});self.assertEqual(r['payments'][0]['status'],'pending');self.assertNotIn('SECRET_PROVIDER',str(r))
   r=await owner.execute(s,self.settings,c,'payment_detail',{'payment_id':str(p.id)});self.assertEqual(r['amount_kopecks'],15000)
   with self.assertRaises(ValueError):await owner.execute(s,self.settings,c,'payment_detail',{'payment_id':'1 OR 1=1'})
   with self.assertRaises(ValueError):await owner.execute(s,self.settings,c,'conversation_detail',{'conversation_id':str(c.id)})
 async def test_schedule_validation_dedup_and_owner_only(self):
  async with self.db.sessions.begin() as s:
   c=await self.oc(s);args={'prompt':'Отчёт по продажам','due_at':(now()+timedelta(hours=1)).isoformat()}
   a=await owner.execute(s,self.settings,c,'schedule_report',args);b=await owner.execute(s,self.settings,c,'schedule_report',args);self.assertEqual(a['task']['id'],b['task']['id'])
   for date in [(now()-timedelta(hours=1)).isoformat(),'2027-01-01T10:00:00']:
    with self.assertRaises(ValueError):await owner.execute(s,self.settings,c,'schedule_report',{**args,'due_at':date})
   other=await service.conversation(s,self.settings,user_id=self.other)
   with self.assertRaises(PermissionError):await owner.execute(s,self.settings,other,'list_tasks',{})
 async def test_scheduled_report_durable_completion_and_readonly_tools(self):
  if not self.pg_schema:self.skipTest('Independent token reservation requires PG')
  async with self.db.sessions.begin() as s:
   c=await self.oc(s);t=AIOwnerTask(owner_id=self.uid,conversation_id=c.id,request_key='task1',prompt='Проанализируй продажи',due_at=now()-timedelta(seconds=1));s.add(t);await s.flush();tid=t.id
  await tasks.tick(self.db,self.settings);await tasks.tick(self.db,self.settings)
  async with self.db.sessions() as s:self.assertEqual(await s.scalar(select(func.count()).select_from(AIJob)),1)
  model=SimpleNamespace(complete=AsyncMock(return_value={'output':[{'type':'message','content':[{'type':'output_text','text':'Отчёт готов.'}]}],'usage':{'total_tokens':20}}))
  await process_one(self.db,self.settings,model);await tasks.tick(self.db,self.settings)
  names={x['name'] for x in model.complete.call_args.args[2]};self.assertNotIn('schedule_report',names);self.assertNotIn('propose_knowledge',names);self.assertIn('sales_report',names)
  async with self.db.sessions() as s:
   self.assertEqual((await s.get(AIOwnerTask,tid)).status,'done');self.assertEqual(await s.scalar(select(func.count()).select_from(AIOutbox)),0)
 async def test_busy_conversation_waits_and_cancel_stops_job(self):
  async with self.db.sessions.begin() as s:
   c=await self.oc(s);await service.append(s,self.settings,c,'Привет','busy');t=AIOwnerTask(owner_id=self.uid,conversation_id=c.id,request_key='task2',prompt='Отчёт',due_at=now()-timedelta(seconds=1));s.add(t);await s.flush();tid=t.id
  await tasks.tick(self.db,self.settings)
  async with self.db.sessions.begin() as s:
   t=await s.get(AIOwnerTask,tid);self.assertEqual(t.status,'scheduled');j=await s.scalar(select(AIJob));j.status='done'
  await tasks.tick(self.db,self.settings)
  async with self.db.sessions.begin() as s:
   t=await s.get(AIOwnerTask,tid);self.assertEqual(t.status,'running');await owner.execute(s,self.settings,await self.oc(s),'cancel_task',{'task_id':str(tid)});self.assertEqual((await s.get(AIJob,t.job_id)).status,'canceled')
  await tasks.tick(self.db,self.settings)
  async with self.db.sessions() as s:self.assertEqual((await s.get(AIOwnerTask,tid)).status,'canceled')
 async def test_attribution_ownership_expiry_and_idempotent_binding(self):
  async with self.db.sessions.begin() as s:
   c=await service.conversation(s,self.settings,browser='BROWSER');url=await attribution.create(s,self.settings,c,False);await s.flush();token=url.rsplit('/',1)[-1]
   req=SimpleNamespace(cookies={'freeman_ai_browser':'BROWSER','freeman_ai_checkout':token})
   with patch.object(attribution,'authenticated',AsyncMock(return_value=None)):
    link=await attribution.owned_link(s,self.settings,req,token);self.assertIsNotNone(link)
    wrong=SimpleNamespace(cookies={'freeman_ai_browser':'OTHER'});self.assertIsNone(await attribution.owned_link(s,self.settings,wrong,token))
    o=GuestOrder(token_hash='o',request_hash='r',months=1,expires_at=now()+timedelta(days=1));s.add(o);await s.flush()
    await attribution.bind_order(s,self.settings,req,o);await s.flush();self.assertEqual(await s.scalar(select(func.count()).select_from(AIOrderAttribution)),0)
    link.clicked_at=now();await attribution.bind_order(s,self.settings,req,o);await s.flush();await attribution.bind_order(s,self.settings,req,o);await s.flush();self.assertEqual(await s.scalar(select(func.count()).select_from(AIOrderAttribution)),1)
    link.expires_at=now()-timedelta(seconds=1);self.assertIsNone(await attribution.owned_link(s,self.settings,req,token))
 async def test_negotiations_only_explicit_paid_attribution(self):
  async with self.db.sessions.begin() as s:
   oc=await self.oc(s);await service.append(s,self.settings,oc,'PRIVATE_OWNER','secret','assistant')
   c=await service.conversation(s,self.settings,user_id=self.other);await service.append(s,self.settings,c,'Нужна инструкция для iOS','client')
   l=AICheckoutLink(conversation_id=c.id,token_hash='a',expires_at=now()+timedelta(days=1),clicked_at=now());s.add(l);await s.flush()
   for i,(status,total,test,linked) in enumerate([('order_paid',10000,False,True),('pending',20000,False,True),('order_paid',30000,True,True),('order_paid',40000,False,False)]):
    p=Payment(user_id=self.other,amount=999,status=status);s.add(p);await s.flush();s.add(AppSetting(key='sale_snapshot:'+str(p.id),value=json.dumps({'total':total,'test':test})))
    o=GuestOrder(user_id=self.other,payment_id=p.id,token_hash=str(i),request_hash=str(i),months=1,expires_at=now()+timedelta(days=1));s.add(o);await s.flush()
    if linked:s.add(AIOrderAttribution(link_id=l.id,order_id=o.id))
   await s.flush();r=await owner.execute(s,self.settings,oc,'negotiations_report',{'period':'day','start':'','end':''})
   self.assertEqual(r['linked_orders'],3);self.assertEqual(r['paid_linked_orders'],1);self.assertEqual(r['linked_revenue_kopecks'],10000);self.assertNotIn('PRIVATE_OWNER',str(r));self.assertEqual(r['conversations'],1)
 async def test_tasks_api_owner_only_and_csrf(self):
  import httpx
  from fastapi import FastAPI
  from app.web.freeman_ai import ai_router
  async def current(token):
   async with self.db.sessions() as s:return await s.get(User,self.uid if token=='owner' else self.other)
  app=FastAPI();app.include_router(ai_router(self.db,self.settings,current))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://example.test') as cl:
   cl.cookies.set('freeman_session','staff');self.assertEqual((await cl.get('/api/web/admin/assistant/tasks')).status_code,403)
   cl.cookies.set('freeman_session','owner');self.assertEqual((await cl.get('/api/web/admin/assistant/tasks')).json(),{'items':[]})
   self.assertEqual((await cl.post('/api/web/admin/assistant/tasks/1/cancel',headers={'Origin':'https://evil.test'})).status_code,403)
