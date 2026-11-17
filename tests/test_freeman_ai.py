"""Offline acceptance: fake model, temporary DB, no provider sends/payments."""
import asyncio,json,os,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from datetime import timedelta
from cryptography.fernet import Fernet
from sqlalchemy import select,func
from app.database.models import *
from app.database.session import Database
from app.database.repositories import SQLRepository
from app.services.freeman_ai import identity,service,tools
from app.services.freeman_ai.security import redact,fingerprint
from app.services.freeman_ai.worker import process_one
from app.services.freeman_ai.channels import valid_signature,ingest_whatsapp,deliver_one

class Model:
    def __init__(self):self.inputs=[]
    async def complete(self,items,instructions,schemas):
        self.inputs.append(items)
        return {'output':[{'type':'message','content':[{'type':'output_text','text':'Помогу с VPN. Какая у вас платформа?'}]}],'usage':{'total_tokens':50}}

class Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.pg_schema=None;self.pg_admin=None
        if os.environ.get('AI_TEST_POSTGRES')=='1':
            import secrets
            from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
            self.pg_schema='ai_test_'+secrets.token_hex(8)
            self.pg_admin=create_async_engine(os.environ['DATABASE_URL'],hide_parameters=True)
            async with self.pg_admin.begin() as c:await c.exec_driver_sql('CREATE SCHEMA '+self.pg_schema)
            self.db=Database(os.environ['DATABASE_URL']);await self.db.engine.dispose()
            self.db.engine=create_async_engine(os.environ['DATABASE_URL'],hide_parameters=True,connect_args={'server_settings':{'search_path':self.pg_schema}})
            self.db.sessions=async_sessionmaker(self.db.engine,expire_on_commit=False)
        else:self.db=Database('sqlite+aiosqlite:///'+self.tmp.name+'/test.db')
        async with self.db.engine.begin() as c:await c.run_sync(Base.metadata.create_all)
        self.settings=SimpleNamespace(encryption_key=Fernet.generate_key().decode(),freeman_ai_enabled=True,freeman_ai_model='fake-test',freeman_ai_daily_token_limit=100000,freeman_ai_retention_days=90,web_base_url='https://example.test',freeman_ai_meta_app_secret='test-only-secret',freeman_ai_whatsapp_phone_id='111',admin_ids='',is_admin=lambda x:False,is_owner=lambda x:False)
        async with self.db.sessions.begin() as s:
            s.add(AppSetting(key='freeman_ai_enabled',value='1'))
            a=User(telegram_id=123,freeman_id='FO-1',first_name='Alice',trial_used=True);b=User(telegram_id=456,freeman_id='FO-2',first_name='Bob')
            s.add_all([a,b]);await s.flush();self.uid=a.id;self.other=b.id
    async def asyncTearDown(self):
        await self.db.close();self.tmp.cleanup()
        if self.pg_admin:
            async with self.pg_admin.begin() as c:await c.exec_driver_sql('DROP SCHEMA '+self.pg_schema+' CASCADE')
            await self.pg_admin.dispose()
    async def prospect(self,s,subject='70000000001'):
        i=await identity.channel_identity(s,self.settings,'whatsapp',subject)
        return i,await service.conversation(s,self.settings,identity=i)
    async def test_unknown_channel_does_not_create_customer(self):
        async with self.db.sessions.begin() as s:
            before=await s.scalar(select(func.count()).select_from(User));i,c=await self.prospect(s)
            self.assertIsNone(c.user_id);self.assertIsNone(await identity.customer(s,i))
            self.assertEqual(before,await s.scalar(select(func.count()).select_from(User)))
            self.assertNotIn('70000000001',i.encrypted_subject)
    async def test_unverified_contact_cannot_claim_account(self):
        async with self.db.sessions.begin() as s:
            s.add(CustomerContact(user_id=self.uid,channel='whatsapp',destination='70000000001',verified=False,enabled=True))
            i,c=await self.prospect(s);self.assertIsNone(c.user_id)
            with self.assertRaises(PermissionError):await tools.execute(s,self.settings,c,'payments',{})
    async def test_link_proof_one_time_and_conflict(self):
        async with self.db.sessions.begin() as s:
            i,c=await self.prospect(s);token=await identity.issue_challenge(s,self.settings,i);iid=i.id;cid=c.id
        async with self.db.sessions.begin() as s:
            await identity.consume_challenge(s,self.settings,await s.get(User,self.uid),token)
            await s.refresh(await s.get(AIConversation,cid));self.assertEqual((await s.get(AIConversation,cid)).user_id,self.uid)
            with self.assertRaises(ValueError):await identity.consume_challenge(s,self.settings,await s.get(User,self.uid),token)
            i=await s.get(ChannelIdentity,iid)
            with self.assertRaises(PermissionError):await identity.bind(s,self.settings,i,await s.get(User,self.other),'test')
    async def test_primary_telegram_reuses_customer_and_revocation_blocks_tools(self):
        async with self.db.sessions.begin() as s:
            i=await identity.channel_identity(s,self.settings,'telegram','123');c=await service.conversation(s,self.settings,identity=i)
            self.assertEqual(c.user_id,self.uid)
            trial=await tools.execute(s,self.settings,c,'trial_eligibility',{});self.assertFalse(trial['eligible'])
            contact=await s.get(CustomerContact,i.contact_id);contact.enabled=False
            with self.assertRaises(PermissionError):await tools.execute(s,self.settings,c,'subscriptions',{})
    async def test_tool_extra_userid_and_unknown_tool_denied(self):
        async with self.db.sessions.begin() as s:
            c=await service.conversation(s,self.settings,user_id=self.uid)
            with self.assertRaises(ValueError):await tools.execute(s,self.settings,c,'payments',{'user_id':self.other})
            with self.assertRaises(PermissionError):await tools.execute(s,self.settings,c,'execute_sql',{})
    async def test_key_scope_and_redaction(self):
        raw='https://sub.example.test/PRIVATE-VPN-KEY'
        async with self.db.sessions.begin() as s:
            server=RemnawaveServer(name='Test',country='RU',api_url='https://provider.invalid',api_token='not-a-real-token')
            s.add(server);await s.flush()
            sub=Subscription(user_id=self.other,expires_at=now()+timedelta(days=3),device_limit=1);s.add(sub);await s.flush()
            s.add(VlessCredential(server_id=server.id,user_id=self.other,subscription_id=sub.id,remote_user_uuid='private-uuid',subscription_url=raw,expires_at=sub.expires_at));await s.flush()
            data=await tools.key_diagnostic(s,raw);self.assertTrue(data['matched']);self.assertFalse(data['ownership_verified'])
            self.assertNotIn('user_id',data);self.assertNotIn('subscription_id',data)
            c=await service.conversation(s,self.settings,browser='a'*43);m=await service.append(s,self.settings,c,'Мой ключ '+raw,'key-test')
            self.assertNotIn(raw,m.text);self.assertNotIn(raw,m.actions);self.assertIsNone(c.user_id)
    # Exercise the dormant engine independently from the production admin-only gate.
    @patch('app.services.freeman_ai.service.channel_enabled',new_callable=AsyncMock,return_value=True)
    async def test_idempotent_message_and_one_response(self,_channel):
        async with self.db.sessions.begin() as s:
            c=await service.conversation(s,self.settings,browser='b'*43);cid=c.id
            a=await service.append(s,self.settings,c,'Привет','unique');b=await service.append(s,self.settings,c,'Привет','unique');self.assertEqual(a.id,b.id)
        model=Model();self.assertTrue(await process_one(self.db,self.settings,model));self.assertFalse(await process_one(self.db,self.settings,model))
        async with self.db.sessions() as s:self.assertEqual(await s.scalar(select(func.count()).select_from(AIMessage).where(AIMessage.conversation_id==cid,AIMessage.role=='assistant')),1)
    # Exercise the dormant engine independently from the production admin-only gate.
    @patch('app.services.freeman_ai.service.channel_enabled',new_callable=AsyncMock,return_value=True)
    async def test_newer_turn_supersedes_old_job(self,_channel):
        async with self.db.sessions.begin() as s:
            c=await service.conversation(s,self.settings,browser='c'*43)
            await service.append(s,self.settings,c,'Привет','first');await service.append(s,self.settings,c,'Нужен VPN','second')
        model=Model();await process_one(self.db,self.settings,model);self.assertEqual(len(model.inputs),0)
        await process_one(self.db,self.settings,model);self.assertEqual(len(model.inputs),1)
    # Exercise the dormant engine independently from the production admin-only gate.
    @patch('app.services.freeman_ai.service.channel_enabled',new_callable=AsyncMock,return_value=True)
    async def test_takeover_during_provider_request_suppresses_answer(self,_channel):
        async with self.db.sessions.begin() as s:
            c=await service.conversation(s,self.settings,browser='d'*43);cid=c.id;await service.append(s,self.settings,c,'Вопрос','takeover')
        started=asyncio.Event();release=asyncio.Event()
        class Delayed(Model):
            async def complete(inner,*args):started.set();await release.wait();return await super().complete(*args)
        task=asyncio.create_task(process_one(self.db,self.settings,Delayed()));await asyncio.wait_for(started.wait(),5)
        async with self.db.sessions.begin() as s:await service.switch_mode(s,self.settings,await s.get(AIConversation,cid),'human')
        release.set();await task
        async with self.db.sessions() as s:
            self.assertEqual((await s.get(AIConversation,cid)).mode,'human')
            self.assertEqual(await s.scalar(select(func.count()).select_from(AIMessage).where(AIMessage.role=='assistant')),0)
    # Exercise the dormant engine independently from the production admin-only gate.
    @patch('app.services.freeman_ai.service.channel_enabled',new_callable=AsyncMock,return_value=True)
    async def test_provider_failure_escalates_without_losing_message(self,_channel):
        async with self.db.sessions.begin() as s:
            c=await service.conversation(s,self.settings,browser='e'*43);cid=c.id;await service.append(s,self.settings,c,'Проблема','failure')
        class Broken:
            async def complete(inner,*args):raise TimeoutError('test')
        await process_one(self.db,self.settings,Broken())
        async with self.db.sessions() as s:self.assertEqual((await s.get(AIConversation,cid)).mode,'human');self.assertEqual((await s.scalar(select(AIJob))).status,'failed')
    async def test_whatsapp_signature_and_replay(self):
        import hmac,hashlib
        raw=b'{"object":"whatsapp_business_account"}'
        sig='sha256='+hmac.new(self.settings.freeman_ai_meta_app_secret.encode(),raw,hashlib.sha256).hexdigest()
        self.assertTrue(valid_signature(self.settings,raw,sig));self.assertFalse(valid_signature(self.settings,raw+b' ',sig))
        payload={'object':'whatsapp_business_account','entry':[{'changes':[{'value':{'metadata':{'phone_number_id':'111'},'messages':[{'id':'wamid.test','from':'70000000002','timestamp':str(int(now().timestamp())),'type':'text','text':{'body':'Привет'}}]}}]}]}
        async with self.db.sessions.begin() as s:
            self.assertEqual(await ingest_whatsapp(s,self.settings,payload),1);self.assertEqual(await ingest_whatsapp(s,self.settings,payload),0)
    @patch('app.services.freeman_ai.channels.channel_enabled',new_callable=AsyncMock,return_value=True)
    async def test_whatsapp_window_and_stale_outbox_no_send(self,_channel):
        async with self.db.sessions.begin() as s:
            i,c=await self.prospect(s);i.last_seen_at=now()-timedelta(hours=25)
            await service.append(s,self.settings,c,'Ответ','old-window','operator')
        self.assertTrue(await deliver_one(self.db,self.settings))
        async with self.db.sessions() as s:self.assertEqual((await s.scalar(select(AIOutbox))).status,'requires_template')
    # Exercise the dormant engine independently from the production admin-only gate.
    @patch('app.services.freeman_ai.service.channel_enabled',new_callable=AsyncMock,return_value=True)
    async def test_daily_budget_rejects_before_model_call(self,_channel):
        self.settings.freeman_ai_daily_token_limit=1
        async with self.db.sessions.begin() as s:
            c=await service.conversation(s,self.settings,browser='f'*43);await service.append(s,self.settings,c,'Вопрос','budget')
        model=Model();await process_one(self.db,self.settings,model);self.assertEqual(len(model.inputs),0)
    async def test_api_account_boundaries_and_cross_origin(self):
        import httpx,time
        from fastapi import FastAPI,HTTPException
        from app.web.freeman_ai import ai_router
        async def current_user(token):
            if token!='staff':raise HTTPException(401,'Required')
            async with self.db.sessions() as s:return await s.get(User,self.uid)
        self.settings.can_admin_action=lambda *args:True
        self.settings.is_owner=lambda *args:True
        self.settings.freeman_ai_openai_key=''
        app=FastAPI();app.include_router(ai_router(self.db,self.settings,current_user))
        async with self.db.sessions.begin() as s:
            c=await service.conversation(s,self.settings,user_id=self.other);cid=c.id
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://example.test') as client:
            self.assertEqual((await client.get('/api/web/admin/ai')).status_code,401)
            self.assertEqual((await client.get('/api/ai/conversations/'+str(cid))).status_code,403)
            token=Fernet(self.settings.encryption_key.encode()).encrypt(json.dumps({'user_id':self.uid,'version':0,'expires':time.time()+3600}).encode()).decode()
            client.cookies.set('freeman_session',token)
            self.assertEqual((await client.get('/api/ai/conversations/'+str(cid))).status_code,403)
            self.assertEqual((await client.post('/api/ai/conversations',json={},headers={'Origin':'https://evil.test'})).status_code,403)
            client.cookies.set('freeman_session','staff')
            self.assertEqual((await client.post('/api/web/admin/assistant/config',json={'enabled':True})).status_code,409)
            self.assertEqual((await client.post('/api/ai/whatsapp/webhook',json={})).status_code,403)
    async def test_retention_and_rate_limit(self):
        from app.services.freeman_ai.limits import cleanup,throttle
        async with self.db.sessions.begin() as s:
            c=await service.conversation(s,self.settings,browser='z'*43)
            m=await service.append(s,self.settings,c,'Старая переписка','retention')
            m.created_at=now()-timedelta(days=100)
            await s.flush();await cleanup(s,self.settings)
            self.assertEqual(m.text,'[Срок хранения истёк]')
            await throttle(s,self.settings,'test',limit=1)
            with self.assertRaises(ValueError):await throttle(s,self.settings,'test',limit=1)
    async def test_expired_identity_challenge(self):
        async with self.db.sessions.begin() as s:
            i,c=await self.prospect(s);token=await identity.issue_challenge(s,self.settings,i);await s.flush()
            challenge=await s.scalar(select(AIIdentityChallenge));challenge.expires_at=now()-timedelta(seconds=1)
            with self.assertRaises(ValueError):await identity.consume_challenge(s,self.settings,await s.get(User,self.uid),token)
    async def test_stale_unsent_assistant_is_canceled(self):
        async with self.db.sessions.begin() as s:
            i,c=await self.prospect(s)
            await service.append(s,self.settings,c,'Устаревший ответ','stale-reply','assistant')
            await service.append(s,self.settings,c,'Новый вопрос','fresh-client')
        self.assertTrue(await deliver_one(self.db,self.settings))
        async with self.db.sessions() as s:self.assertEqual((await s.scalar(select(AIOutbox))).status,'canceled')
    async def test_existing_support_handoff_and_operator_bridge(self):
        from app.services.tickets import TicketService
        self.settings.can_admin_action=lambda *args:True
        async with self.db.sessions.begin() as s:
            c=await service.conversation(s,self.settings,user_id=self.uid)
            await service.append(s,self.settings,c,'Позовите оператора','support-handoff')
            self.assertIsNotNone(c.ticket_id)
            # The initial customer turn is contained once in the handoff summary.
            self.assertEqual(await s.scalar(select(func.count()).select_from(TicketMessage).where(TicketMessage.ticket_id==c.ticket_id)),1)
            await TicketService(SQLRepository(s),self.settings).input('admin:t:input:reply:'+str(c.ticket_id),await s.get(User,self.other),'Здравствуйте, помогу.','operator-bridge')
            await s.flush()
            self.assertEqual(await s.scalar(select(func.count()).select_from(AIMessage).where(AIMessage.conversation_id==c.id,AIMessage.role=='operator')),1)
            await TicketService(SQLRepository(s),self.settings).input('t:input:reply:'+str(c.ticket_id),await s.get(User,self.uid),'Спасибо','customer-bridge')
            await s.flush()
            self.assertEqual(await s.scalar(select(func.count()).select_from(TicketMessage).where(TicketMessage.ticket_id==c.ticket_id,TicketMessage.text=='Спасибо')),1)
    # Exercise the dormant engine independently from the production admin-only gate.
    @patch('app.services.freeman_ai.service.channel_enabled',new_callable=AsyncMock,return_value=True)
    async def test_multistep_model_calls_real_read_tool(self,_channel):
        if not self.pg_schema:self.skipTest('Production PostgreSQL transaction test')
        self.settings.yookassa_shop_id='';self.settings.yookassa_secret_key='';self.settings.yookassa_test_mode=True
        async with self.db.sessions.begin() as s:
            s.add(Tariff(months=1,price=12345,device_limit=2))
            c=await service.conversation(s,self.settings,browser='m'*43);await service.append(s,self.settings,c,'Какие тарифы?','multi-tool')
        class ToolModel(Model):
            async def complete(inner,items,*args):
                if not inner.inputs:
                    inner.inputs.append(items)
                    return {'output':[{'type':'function_call','name':'catalog','arguments':'{}','call_id':'call_test'}],'usage':{'total_tokens':60}}
                data=json.loads([i['output'] for i in items if i.get('type')=='function_call_output'][-1])
                self.assertEqual(data['tariffs'][0]['amount_kopecks'],12345)
                return await super().complete(items,*args)
        model=ToolModel();await process_one(self.db,self.settings,model)
        async with self.db.sessions() as s:self.assertEqual((await s.scalar(select(AIJob))).status,'done')
    async def test_redaction(self):
        value=redact('vless://private@example.com:443 пароль=123 secret https://secret.test/key email@example.com +79991234567')
        for secret in ('vless://','https://','email@example.com','123','79991234567'):self.assertNotIn(secret,value)

if __name__=='__main__':unittest.main()
