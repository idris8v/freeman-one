import unittest,json
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from sqlalchemy import select,func
from test_freeman_ai import Tests as Fixture,Model
from app.database.models import User,Ticket,AIKnowledge,AIConversation,AIMessage,AIOutbox,Payment,AppSetting,now
from app.services.freeman_ai import owner,service,tools
from app.services.freeman_ai.worker import process_one

class OwnerTests(unittest.IsolatedAsyncioTestCase):
 asyncTearDown=Fixture.asyncTearDown
 async def asyncSetUp(self):
  await Fixture.asyncSetUp(self)
  self.settings.is_owner=lambda tid:tid==123
  self.settings.can_admin_action=lambda *args:True
  self.settings.freeman_ai_openai_key='test-only'
  self.settings.freeman_ai_whatsapp_token=''
  self.settings.freeman_ai_telegram_enabled=False
  self.settings.freeman_ai_whatsapp_enabled=False
 async def test_broadcast_proposal_is_visible_and_does_not_send(self):
  async with self.db.sessions.begin() as s:
   c=await owner.conversation(s,self.settings,await s.get(User,self.uid))
   args=dict(title='Новости',message='Плановое обновление',channels='telegram,internal',audience='selected',user_ids=str(self.other))
   result=await owner.execute(s,self.settings,c,'prepare_broadcast',args)
   self.assertFalse(result['sent']);self.assertTrue(result['requires_owner_confirmation'])
   m=await service.append(s,self.settings,c,'Подготовил','broadcast-test','assistant',result['actions'])
   self.assertEqual(await s.scalar(select(func.count()).select_from(AIOutbox)),0)
   self.assertEqual(result['actions'][0]['request_id'],(await owner.execute(s,self.settings,c,'prepare_broadcast',args))['actions'][0]['request_id'])
   with self.assertRaises(ValueError):await owner.execute(s,self.settings,c,'prepare_broadcast',{**args,'channels':'unknown'})
 async def test_proposal_requires_owner_confirmation_and_is_idempotent(self):
  async with self.db.sessions.begin() as s:
   u=await s.get(User,self.uid);c=await owner.conversation(s,self.settings,u)
   result=await owner.execute(s,self.settings,c,'propose_knowledge',{'title':'Подключение','text':'Откройте приложение.'})
   self.assertEqual(await s.scalar(select(func.count()).select_from(AIKnowledge)),0)
   m=await service.append(s,self.settings,c,'Предлагаю инструкцию','proposal','assistant',result['actions'])
   with self.assertRaises(PermissionError):await owner.confirm(s,self.settings,await s.get(User,self.other),m.id,0)
   kid=await owner.confirm(s,self.settings,u,m.id,0)
   self.assertEqual(kid,await owner.confirm(s,self.settings,u,m.id,0));self.assertTrue((await s.get(AIKnowledge,kid)).enabled)
   self.assertEqual(await s.scalar(select(func.count()).select_from(AIOutbox)),0)
 async def test_customer_cannot_call_owner_tools(self):
  async with self.db.sessions.begin() as s:
   c=await service.conversation(s,self.settings,user_id=self.uid)
   with self.assertRaises(PermissionError):await owner.execute(s,self.settings,c,'read_knowledge',{})
   with self.assertRaises((PermissionError,ValueError)):await tools.execute(s,self.settings,c,'sales_report',{})
 @patch('app.services.freeman_ai.service.channel_enabled',new_callable=AsyncMock,return_value=True)
 async def test_owner_history_never_enters_customer_context(self,_channel):
  async with self.db.sessions.begin() as s:
   c=await owner.conversation(s,self.settings,await s.get(User,self.uid))
   await service.append(s,self.settings,c,'PRIVATE_FINANCES','private','assistant')
   c=await service.conversation(s,self.settings,user_id=self.uid);await service.append(s,self.settings,c,'Привет','web')
  model=Model();model.complete=AsyncMock(return_value={'output':[{'type':'message','content':[{'type':'output_text','text':'Привет'}]}]})
  await process_one(self.db,self.settings,model)
  self.assertNotIn('PRIVATE_FINANCES',str(model.complete.call_args))
 async def test_owner_failure_does_not_create_customer_ticket(self):
  async with self.db.sessions.begin() as s:
   c=await owner.conversation(s,self.settings,await s.get(User,self.uid));await service.append(s,self.settings,c,'Оператор продаж','owner-query');cid=c.id
  model=SimpleNamespace(complete=AsyncMock(side_effect=TimeoutError))
  await process_one(self.db,self.settings,model)
  async with self.db.sessions() as s:
   self.assertEqual(await s.scalar(select(func.count()).select_from(Ticket)),0)
   self.assertEqual((await s.get(AIConversation,cid)).mode,'ai')
 async def test_channel_activation_website_only(self):
  async with self.db.sessions() as s:
   self.assertFalse(await service.channel_enabled(s,self.settings,'website'))
   self.assertTrue(await service.channel_enabled(s,self.settings,'owner'))
   self.assertFalse(await service.channel_enabled(s,self.settings,'telegram'))
   self.assertFalse(await service.channel_enabled(s,self.settings,'whatsapp'))
 async def test_owner_api_isolation(self):
  import httpx
  from fastapi import FastAPI,HTTPException
  from app.web.freeman_ai import ai_router
  async def current(token):
   if token not in ('owner','staff'):raise HTTPException(401)
   async with self.db.sessions() as s:return await s.get(User,self.uid if token=='owner' else self.other)
  app=FastAPI();app.include_router(ai_router(self.db,self.settings,current))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://example.test') as cl:
   cl.cookies.set('freeman_session','owner');r=await cl.post('/api/web/admin/assistant');self.assertEqual(r.status_code,200);cid=r.json()['id']
   r=await cl.post(f'/api/web/admin/assistant/chats/{cid}/messages',json={'text':'Привет','request_id':'owner-request-12345'});self.assertEqual(r.status_code,200)
   cl.cookies.set('freeman_session','staff')
   self.assertEqual((await cl.post('/api/web/admin/assistant')).status_code,403)
   self.assertEqual((await cl.get(f'/api/web/admin/assistant/chats/{cid}')).status_code,403)
   self.assertEqual((await cl.get('/api/web/admin/ai')).json()['items'],[])
   self.assertEqual((await cl.get(f'/api/ai/conversations/{cid}')).status_code,403)
   self.assertEqual((await cl.post('/api/web/admin/assistant/confirm',json={'message_id':1,'index':0})).status_code,403)
 async def test_owner_tool_roundtrip_creates_only_proposal(self):
  async with self.db.sessions.begin() as s:
   c=await owner.conversation(s,self.settings,await s.get(User,self.uid));await service.append(s,self.settings,c,'Добавь инструкцию','new-instruction');cid=c.id
  model=SimpleNamespace(complete=AsyncMock(side_effect=[{'output':[{'type':'function_call','name':'propose_knowledge','call_id':'a','arguments':json.dumps({'title':'FAQ','text':'Проверьте подключение.'})}]},{'output':[{'type':'message','content':[{'type':'output_text','text':'Подтвердите инструкцию.'}]}]}]))
  await process_one(self.db,self.settings,model)
  async with self.db.sessions() as s:
   c=await s.get(AIConversation,cid);messages=await service.messages(s,c)
   self.assertEqual(messages[-1]['actions'][0]['type'],'knowledge_proposal')
   self.assertEqual(await s.scalar(select(func.count()).select_from(AIKnowledge)),0)
 async def test_sales_report_uses_snapshots_without_customer_details(self):
  if not self.pg_schema:self.skipTest('PostgreSQL report SQL')
  async with self.db.sessions.begin() as s:
   p=Payment(user_id=self.uid,amount=10000,status='manual_paid');s.add(p);await s.flush()
   s.add(AppSetting(key='sale_snapshot:'+str(p.id),value=json.dumps({'months':1,'devices':1,'total':12000,'kind':'vpn','renewal':False,'paid_at':now().isoformat()})));await s.flush()
   c=await owner.conversation(s,self.settings,await s.get(User,self.uid))
   result=await owner.execute(s,self.settings,c,'sales_report',{'period':'day','start':'','end':'','kind':'vpn'})
   self.assertEqual(result['report']['stats']['revenue'],12000)
   raw=json.dumps(result);self.assertNotIn('Alice',raw);self.assertNotIn('items',result['report']);self.assertEqual(result['money_unit'],'kopecks')
 async def test_active_vpn_broadcast_uses_server_filter(self):
  async with self.db.sessions.begin() as s:
   c=await owner.conversation(s,self.settings,await s.get(User,self.uid))
   r=await owner.execute(s,self.settings,c,'prepare_broadcast',dict(title='Новость',message='Обновление сервиса',channels='Telegram',audience='active_vpn',user_ids=''))
   action=r['actions'][0]
   self.assertEqual(action['channels'],['telegram']);self.assertEqual(action['filters'],{'key_state':'active'});self.assertEqual(action['user_ids'],[])
   self.assertFalse(r['sent']);self.assertEqual(await s.scalar(select(func.count()).select_from(AIOutbox)),0)
 async def test_owner_context_allows_large_tool_schema(self):
  from app.services.freeman_ai.limits import reserve,settle
  self.settings.freeman_ai_daily_token_limit=1000000
  async with self.db.sessions() as s:
   items=[{'role':'user','content':'а'*35000}]
   with self.assertRaisesRegex(ValueError,'context_budget_exceeded'):await reserve(s,self.settings,items,[])
   reservation=await reserve(s,self.settings,items,[],context_limit=200000)
   await settle(s,reservation,100)
