import unittest
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from app.database.models import Base,User
from app.services.accounts import find_freeman_id,display_freeman_id

class FreemanIdentityTests(unittest.IsolatedAsyncioTestCase):
 async def test_aliases_resolve_stored_identity_not_database_id(self):
  engine=create_async_engine('sqlite+aiosqlite:///:memory:')
  try:
   async with engine.begin() as connection:await connection.run_sync(Base.metadata.create_all)
   sessions=async_sessionmaker(engine,expire_on_commit=False)
   async with sessions.begin() as session:
    user=User(id=7,freeman_id='FRM-000028');session.add(user)
   async with sessions() as session:
    for alias in ['FO-28','FRM-000028','Freeman 28']:
     self.assertEqual((await find_freeman_id(session,alias)).id,7)
     self.assertEqual(display_freeman_id(alias),'FO-28')
    self.assertIsNone(await find_freeman_id(session,'FO-7'))
  finally:await engine.dispose()

 async def test_assistant_finds_new_and_legacy_stored_ids(self):
  from unittest.mock import AsyncMock,patch
  from app.services.freeman_ai.insights import execute
  engine=create_async_engine('sqlite+aiosqlite:///:memory:')
  try:
   async with engine.begin() as connection:await connection.run_sync(Base.metadata.create_all)
   sessions=async_sessionmaker(engine,expire_on_commit=False)
   async with sessions.begin() as session:
    session.add_all([User(id=7,freeman_id='FRM-000028'),User(id=8,freeman_id='FO-29')])
   with patch('app.services.freeman_ai.owner.authorize',new=AsyncMock()):
    async with sessions() as session:
     for query,uid in [('FO-28',7),('FRM-000028',7),('Freeman 28',7),('FRM-000029',8),('FO-29',8)]:
      result=await execute(session,None,None,'find_client',{'query':query})
      self.assertEqual([u['id'] for u in result['clients']],[uid])
  finally:await engine.dispose()
