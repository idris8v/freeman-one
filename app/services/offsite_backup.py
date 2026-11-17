"""Daily encrypted database copy delivered privately to the owner."""
import asyncio
import logging
from datetime import datetime, timezone
from cryptography.fernet import Fernet
from aiogram.types import BufferedInputFile
from app.database.repositories import SQLRepository
from app.utils.backup import backup


class OffsiteBackupWorker:
    def __init__(self, database, bot, settings):
        self.database, self.bot, self.settings = database, bot, settings

    async def run_once(self):
        if not self.settings.encryption_key or not self.settings.owner_ids():
            return False
        today=datetime.now(timezone.utc).date().isoformat()
        async with self.database.sessions.begin() as session:
            repo=SQLRepository(session)
            if await repo.setting('backup_offsite_enabled','1')!='1' or await repo.setting('backup_offsite_last')==today:
                return False
        path=await asyncio.to_thread(backup)
        encrypted=Fernet(self.settings.encryption_key.encode()).encrypt(await asyncio.to_thread(path.read_bytes))
        delivered=False
        for telegram_id in self.settings.owner_ids():
            document=BufferedInputFile(encrypted,filename=f'freeman-backup-{today}{path.suffix}.fernet')
            try:
                await self.bot.send_document(telegram_id,document,caption='🔐 Ежедневная зашифрованная резервная копия F R E E M A N. Для восстановления нужен ENCRYPTION_KEY сервера.')
                delivered=True
            except Exception:
                logging.getLogger(__name__).exception('Offsite backup delivery failed for owner %s',telegram_id)
        if delivered:
            async with self.database.sessions.begin() as session:
                await SQLRepository(session).set_setting('backup_offsite_last',today)
        return delivered

    async def run(self):
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception('Offsite backup cycle failed')
            await asyncio.sleep(3600)
