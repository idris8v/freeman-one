import asyncio
import unittest
from types import SimpleNamespace
from app.services.avatar import TelegramAvatars

class AvatarTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_singleflight_and_canceled_waiter(self):
        avatars=TelegramAvatars();release=asyncio.Event();started=asyncio.Event();calls=[]
        class Bot:
            def __init__(self, token): self.session=self
            async def close(self): pass
            async def get_user_profile_photos(self, uid, limit):
                calls.append(uid)
                if len(calls)==4: started.set()
                await release.wait()
                return SimpleNamespace(photos=[[SimpleNamespace(width=320,file_id=str(uid))]])
            async def download(self, file_id, destination): destination.write(b'\xff\xd8\xffphoto')
        tasks=[asyncio.create_task(avatars.get(uid,'test',Bot)) for uid in [1,1,2,3,4]]
        await asyncio.wait_for(started.wait(),1)
        tasks[0].cancel()
        with self.assertRaises(asyncio.CancelledError): await tasks[0]
        release.set()
        self.assertEqual(await asyncio.gather(*tasks[1:]),[b'\xff\xd8\xffphoto']*4)
        self.assertEqual(calls.count(1),1)
        self.assertEqual(await avatars.get(1,'test',Bot),b'\xff\xd8\xffphoto')
        self.assertEqual(len(calls),4);self.assertFalse(avatars.inflight)

    async def test_missing_and_failed_images_use_negative_cache(self):
        avatars=TelegramAvatars();calls=[]
        class Bot:
            def __init__(self, token): self.session=self
            async def close(self): pass
            async def get_user_profile_photos(self, uid, limit):
                calls.append(uid)
                if uid==1: raise RuntimeError('unavailable')
                return SimpleNamespace(photos=[])
        for uid in [1,2]:
            self.assertIsNone(await avatars.get(uid,'test',Bot))
            self.assertIsNone(await avatars.get(uid,'test',Bot))
        self.assertEqual(calls,[1,2]);self.assertFalse(avatars.inflight)
