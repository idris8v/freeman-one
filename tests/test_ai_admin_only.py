import unittest
from fastapi import FastAPI,HTTPException
import httpx
from test_ai_owner import OwnerTests
from app.database.models import User
from app.web.freeman_ai import ai_router
from app.services.freeman_ai import service
class AdminOnly(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=OwnerTests.asyncSetUp
 asyncTearDown=OwnerTests.asyncTearDown
 async def test_client_routes_closed_owner_available(self):
  async def current(token):
   if not token:raise HTTPException(401)
   async with self.db.sessions() as s:return await s.get(User,self.uid if token=='owner' else self.other)
  app=FastAPI();app.include_router(ai_router(self.db,self.settings,current))
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://example.test') as cl:
   self.assertEqual((await cl.get('/ai')).status_code,303)
   self.assertFalse((await cl.get('/api/ai/status')).json()['enabled'])
   for path,method in [('/api/ai/conversations','POST'),('/api/ai/conversations/1','GET'),('/api/ai/conversations/1/messages','POST'),('/api/ai/identities/link','POST')]:self.assertEqual((await cl.request(method,path,json={})).status_code,403)
   self.assertEqual((await cl.get('/ai/checkout/old')).status_code,410)
   cl.cookies.set('freeman_session','owner');self.assertEqual((await cl.post('/api/web/admin/assistant')).status_code,200)
  async with self.db.sessions() as s:
   self.assertTrue(await service.channel_enabled(s,self.settings,'owner'))
   for channel in ['website','telegram','whatsapp']:self.assertFalse(await service.channel_enabled(s,self.settings,channel))
