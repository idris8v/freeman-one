"""Authenticated Telegram avatars with bounded caching and concurrent loading."""
import asyncio
import time
from collections import OrderedDict
from io import BytesIO

class TelegramAvatars:
    def __init__(self, ttl=900, maximum=256):
        self.ttl, self.maximum = ttl, maximum
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()
        self.inflight = {}
        self.downloads = asyncio.Semaphore(4)

    async def get(self, telegram_id, token, bot_factory=None):
        if not telegram_id or not token:
            return None
        async with self.lock:
            cached = self.cache.get(telegram_id)
            if cached and cached[0] > time.monotonic():
                self.cache.move_to_end(telegram_id)
                return cached[1]
            task = self.inflight.get(telegram_id)
            if task is None:
                task = asyncio.create_task(self._load(telegram_id, token, bot_factory))
                self.inflight[telegram_id] = task
        # A canceled image request must not cancel another request for the same photo.
        return await asyncio.shield(task)

    async def _load(self, telegram_id, token, bot_factory):
        image = None
        async with self.downloads:
            bot = None
            try:
                async with asyncio.timeout(8):
                    if bot_factory is None:
                        from aiogram import Bot
                        bot_factory = Bot
                    bot = bot_factory(token)
                    photos = await bot.get_user_profile_photos(telegram_id, limit=1)
                    if photos.photos:
                        photo = min(photos.photos[0], key=lambda p: abs(p.width - 320))
                        output = BytesIO()
                        await bot.download(photo.file_id, destination=output)
                        data = output.getvalue()
                        if len(data) <= 2_000_000 and data.startswith(b'\xff\xd8\xff'):
                            image = data
            except Exception:
                # Never expose token-bearing Telegram URLs to logs or clients.
                pass
            finally:
                if bot is not None:
                    try:
                        await bot.session.close()
                    except Exception:
                        pass
        async with self.lock:
            self.cache[telegram_id] = (time.monotonic() + (self.ttl if image else 60), image)
            self.cache.move_to_end(telegram_id)
            while len(self.cache) > self.maximum:
                self.cache.popitem(last=False)
            self.inflight.pop(telegram_id, None)
        return image
