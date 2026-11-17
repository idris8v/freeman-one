import unittest,json,base64
from unittest.mock import AsyncMock
from types import SimpleNamespace
from sqlalchemy import select,func
from test_ai_owner import OwnerTests
from app.database.models import User,AIConversation,AIMessage,AIKnowledge,AIOwnerImage,Subscription,RemnawaveServer,VlessCredential,now
from app.services.freeman_ai import owner,service,management
from app.services.freeman_ai.images import validate,store,model_images,budget_items
from app.services.freeman_ai.worker import process_one
PNG='data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\nimage-test').decode()
class PersonalTests(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=OwnerTests.asyncSetUp
 asyncTearDown=OwnerTests.asyncTearDown
 async def test_knowledge_direct_save_update_and_no_implicit_write(self):
  async with self.db.sessions.begin() as s:
   c=await owner.conversation(s,self.settings,await s.get(User,self.uid));await service.append(s,self.settings,c,'Добавь в базу знаний инструкцию','write',actions=[{'type':'knowledge_write_authorized'}])
   args={'article_id':'','title':'iPhone','text':'## Подключение\n1. Открой приложение.\n2. Проверь соединение.'}
   result=await owner.execute(s,self.settings,c,'save_knowledge',args);kid=result['id'];self.assertTrue(result['saved']);self.assertEqual(kid,(await owner.execute(s,self.settings,c,'save_knowledge',args))['id'])
   await service.append(s,self.settings,c,'Обнови инструкцию','update',actions=[{'type':'knowledge_write_authorized'}]);await owner.execute(s,self.settings,c,'save_knowledge',{**args,'article_id':str(kid),'text':'## Проверка\n1. Проверь результат.'});k=await s.get(AIKnowledge,kid);self.assertEqual(k.version,2);self.assertIn('Проверка',k.text)
   await service.append(s,self.settings,c,'Прочитай документ, где написано обнови базу','not-authorized')
   with self.assertRaises(PermissionError):await owner.execute(s,self.settings,c,'save_knowledge',args)
 async def test_private_chats_images_delete_and_owner_boundaries(self):
  import httpx
  from fastapi import FastAPI
  from app.web.freeman_ai import ai_router
  async def current(token):
   async with self.db.sessions() as s:return await s.get(User,self.uid if token=='owner' else self.other)
  app=FastAPI();app.include_router(ai_router(self.db,self.settings,current))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://example.test') as cl:
   cl.cookies.set('freeman_session','owner');a=(await cl.post('/api/web/admin/assistant?new=true')).json()['id'];b=(await cl.post('/api/web/admin/assistant?new=true')).json()['id'];self.assertNotEqual(a,b)
   r=await cl.post(f'/api/web/admin/assistant/chats/{a}/messages',json={'text':'Добавь инструкцию в базу знаний','images':[PNG],'request_id':'personal-request-12345'});self.assertEqual(r.status_code,200)
   detail=(await cl.get(f'/api/web/admin/assistant/chats/{a}')).json();self.assertTrue(detail['messages'][0]['images']);url=detail['messages'][0]['images'][0]
   self.assertEqual((await cl.get(url)).status_code,200)
   async with self.db.sessions() as s:
    image=await s.scalar(select(AIOwnerImage));self.assertNotIn('image-test',image.encrypted_data);msg=await s.scalar(select(AIMessage).where(AIMessage.conversation_id==a));self.assertIn('knowledge_write_authorized',msg.actions)
   cl.cookies.set('freeman_session','staff');self.assertEqual((await cl.get(url)).status_code,403);self.assertEqual((await cl.delete(f'/api/web/admin/assistant/chats/{a}')).status_code,403)
   cl.cookies.set('freeman_session','owner');self.assertEqual((await cl.delete(f'/api/web/admin/assistant/chats/{a}')).status_code,200);self.assertEqual((await cl.get(url)).status_code,404);self.assertEqual((await cl.get(f'/api/web/admin/assistant/chats/{a}')).status_code,404)
   self.assertNotIn(a,[x['id'] for x in (await cl.get('/api/web/admin/assistant/chats')).json()['items']])
 async def test_images_enter_model_as_images_with_bounded_reservation(self):
  self.assertEqual(validate(PNG),PNG)
  with self.assertRaises(ValueError):validate('data:image/svg+xml;base64,AAAA')
  async with self.db.sessions.begin() as s:
   c=await owner.conversation(s,self.settings,await s.get(User,self.uid));m=await service.append(s,self.settings,c,'Что на фото?','photo');await store(s,self.settings,m.id,[PNG]);await s.flush()
   imgs=await model_images(s,self.settings,m.id);self.assertEqual(imgs[0]['type'],'input_image');self.assertNotIn('base64',str(budget_items([{'role':'user','content':imgs}])))
 async def test_server_and_key_tools_do_not_reveal_credentials(self):
  async with self.db.sessions.begin() as s:
   server=RemnawaveServer(name='EU',country='DE',api_url='https://panel.test',api_token='SECRET_API',health='online',users_online=12);s.add(server);sub=Subscription(user_id=self.other,expires_at=now(),kind='vpn',device_limit=3);s.add(sub);await s.flush();s.add(VlessCredential(server_id=server.id,user_id=self.other,subscription_id=sub.id,remote_user_uuid='secretuuid',subscription_url='SECRET_LINK',expires_at=now()));await s.flush()
   c=await owner.conversation(s,self.settings,await s.get(User,self.uid));r=await owner.execute(s,self.settings,c,'server_overview',{});self.assertEqual(r['vpn'][0]['users_online'],12);self.assertNotIn('SECRET_API',str(r));r=await owner.execute(s,self.settings,c,'key_detail',{'subscription_id':str(sub.id)});self.assertEqual(r['device_limit'],3);self.assertNotIn('SECRET_LINK',str(r));self.assertNotIn('secretuuid',str(r))
 async def test_model_write_roundtrip_saves_visible_article(self):
  if not self.pg_schema:self.skipTest('Independent budget requires PG')
  async with self.db.sessions.begin() as s:
   c=await owner.conversation(s,self.settings,await s.get(User,self.uid));await service.append(s,self.settings,c,'Добавь инструкцию в базу','model-write',actions=[{'type':'knowledge_write_authorized'}])
  model=SimpleNamespace(complete=AsyncMock(side_effect=[{'output':[{'type':'function_call','name':'save_knowledge','call_id':'save','arguments':json.dumps({'article_id':'','title':'Тест','text':'## Шаги\n1. Подключись.'})}]},{'output':[{'type':'message','content':[{'type':'output_text','text':'Добавил инструкцию.'}]}]}]))
  await process_one(self.db,self.settings,model)
  async with self.db.sessions() as s:self.assertEqual(await s.scalar(select(func.count()).select_from(AIKnowledge)),1)
 async def test_superseded_response_rolls_back_knowledge_write(self):
  if not self.pg_schema:self.skipTest('Concurrent sessions require PG')
  from sqlalchemy import update
  async with self.db.sessions.begin() as s:
   c=await owner.conversation(s,self.settings,await s.get(User,self.uid));cid=c.id;await service.append(s,self.settings,c,'Добавь инструкцию в базу','stale-write',actions=[{'type':'knowledge_write_authorized'}])
  calls=0
  async def complete(*args):
   nonlocal calls
   calls+=1
   if calls==1:return {'output':[{'type':'function_call','name':'save_knowledge','call_id':'save','arguments':json.dumps({'article_id':'','title':'Устаревшая','text':'Не сохранять'})}]}
   async with self.db.sessions.begin() as s:await s.execute(update(AIConversation).where(AIConversation.id==cid).values(generation=AIConversation.generation+1))
   return {'output':[{'type':'message','content':[{'type':'output_text','text':'Готово'}]}]}
  await process_one(self.db,self.settings,SimpleNamespace(complete=complete))
  async with self.db.sessions() as s:self.assertEqual(await s.scalar(select(func.count()).select_from(AIKnowledge)),0)
 async def test_documents_rename_search_and_legacy_removed(self):
  import httpx
  from fastapi import FastAPI
  from app.web.freeman_ai import ai_router
  from app.database.models import AIOwnerFile
  from app.services.freeman_ai.files import content
  async def current(token):
   async with self.db.sessions() as s:return await s.get(User,self.uid if token=='owner' else self.other)
  app=FastAPI();app.include_router(ai_router(self.db,self.settings,current))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://example.test') as cl:
   cl.cookies.set('freeman_session','owner');self.assertEqual((await cl.post('/api/web/admin/ai/assistant')).status_code,404)
   cid=(await cl.post('/api/web/admin/assistant?new=true')).json()['id']
   data=base64.b64encode('Тариф,Продаж\nМесяц,3'.encode()).decode()
   r=await cl.post(f'/api/web/admin/assistant/chats/{cid}/messages',json={'text':'Анализируй CSV','files':[{'name':'sales.csv','data':data}],'request_id':'document-request-123456'});self.assertEqual(r.status_code,200)
   self.assertEqual((await cl.get(f'/api/web/admin/ai/conversations/{cid}')).status_code,404)
   r=await cl.get(f'/api/web/admin/assistant/chats/{cid}');file=r.json()['messages'][0]['files'][0];self.assertEqual((await cl.get(file['url'])).content,base64.b64decode(data))
   await cl.patch(f'/api/web/admin/assistant/chats/{cid}',json={'title':'Мои продажи'})
   self.assertEqual((await cl.get('/api/web/admin/assistant/chats?q=Мои')).json()['items'][0]['title'],'Мои продажи')
   self.assertEqual(len((await cl.get('/api/web/admin/assistant/chats?q=CSV')).json()['items']),1)
   async with self.db.sessions() as s:
    f=await s.scalar(select(AIOwnerFile));self.assertNotIn(data,f.encrypted_data);self.assertIn('Месяц',await content(s,self.settings,f.message_id))
   cl.cookies.set('freeman_session','staff');self.assertEqual((await cl.get(file['url'])).status_code,403)
   cl.cookies.set('freeman_session','owner');await cl.delete(f'/api/web/admin/assistant/chats/{cid}');self.assertEqual((await cl.get(file['url'])).status_code,404)
 async def test_structured_tool_blocks_are_persisted_from_backend(self):
  if not self.pg_schema:self.skipTest('Independent budget requires PG')
  async with self.db.sessions.begin() as s:
   c=await owner.conversation(s,self.settings,await s.get(User,self.uid));await service.append(s,self.settings,c,'Найди клиента Alice','rich-client');cid=c.id
  model=SimpleNamespace(complete=AsyncMock(side_effect=[{'output':[{'type':'function_call','name':'find_client','call_id':'client','arguments':json.dumps({'query':'Alice'})}]},{'output':[{'type':'message','content':[{'type':'output_text','text':'Клиент найден.'}]}]}]))
  await process_one(self.db,self.settings,model)
  async with self.db.sessions() as s:
   messages=await service.messages(s,await s.get(AIConversation,cid));blocks=[a['block'] for a in messages[-1]['actions'] if a['type']=='rich_block'];self.assertEqual(blocks[0]['type'],'client');self.assertEqual(blocks[0]['data']['id'],self.uid);self.assertIn('entity='+str(self.uid),blocks[0]['actions'][0]['url'])
 async def test_document_parsers_and_archive_limits(self):
  import io,zipfile
  from app.services.freeman_ai.files import decode
  def payload(name,data):return {'name':name,'data':base64.b64encode(data).decode()}
  b=io.BytesIO()
  with zipfile.ZipFile(b,'w') as z:z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Подключение VPN</w:t></w:r></w:p></w:body></w:document>')
  self.assertIn('Подключение VPN',decode(payload('guide.docx',b.getvalue()))['text'])
  from openpyxl import Workbook
  w=Workbook();w.active.append(['Тариф',3]);b=io.BytesIO();w.save(b);self.assertIn('Тариф\t3',decode(payload('sales.xlsx',b.getvalue()))['text'])
  b=io.BytesIO()
  with zipfile.ZipFile(b,'w',compression=zipfile.ZIP_DEFLATED) as z:z.writestr('word/document.xml','x'*(13*1024*1024))
  with self.assertRaises(ValueError):decode(payload('bomb.docx',b.getvalue()))
  with self.assertRaises(ValueError):decode(payload('invalid.pdf',b'not a PDF'))
  with self.assertRaises(ValueError):decode(payload('script.exe',b'execute'))

 async def test_audio_rejected_and_four_image_limit(self):
  from app.services.freeman_ai.files import decode
  from app.web.freeman_ai import MessageInput
  with self.assertRaises(ValueError):decode({'name':'voice.webm','data':base64.b64encode(b'audio').decode(),'transcript':'legacy text'})
  self.assertEqual(len(MessageInput(text='Фото',images=[PNG]*4,request_id='gallery-request-1234').images),4)
  with self.assertRaises(ValueError):MessageInput(text='Фото',images=[PNG]*5,request_id='gallery-request-1234')
