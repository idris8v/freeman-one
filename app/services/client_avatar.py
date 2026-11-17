"""Optional Telegram avatars; failures never block the customer card."""
import asyncio,time
from aiogram.exceptions import TelegramAPIError
cache={}
async def attach(bot,screen):
    uid=screen.avatar_telegram_id
    if not uid or screen.photo or screen.video or screen.animation:return
    entry=cache.get(uid)
    if entry and entry[0]>time.monotonic():
        screen.photo=entry[1];return
    try:
        result=await asyncio.wait_for(bot.get_user_profile_photos(uid,limit=1),timeout=3)
        photo=result.photos[0][-1].file_id if result.photos else None
        cache[uid]=(time.monotonic()+600,photo)
        screen.photo=photo
    except (TelegramAPIError,TimeoutError):
        cache[uid]=(time.monotonic()+60,None)
