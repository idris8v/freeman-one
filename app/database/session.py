from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class Database:
    def __init__(self, url):
        if url.startswith('sqlite') and ':memory:' not in url:
            Path(url.split('///', 1)[1]).parent.mkdir(parents=True, exist_ok=True)
        options={'pool_pre_ping':True,'hide_parameters':True}
        if url.startswith('postgresql'):
            options.update(pool_size=5,max_overflow=5,pool_timeout=15,pool_recycle=1800,connect_args={'server_settings':{'timezone':'UTC','application_name':'freemanone'}})
        self.engine = create_async_engine(url, **options)
        if url.startswith('sqlite'):
            @event.listens_for(self.engine.sync_engine, 'connect')
            def configure(connection, _):
                cursor = connection.cursor()
                cursor.execute('PRAGMA foreign_keys=ON')
                cursor.execute('PRAGMA busy_timeout=10000')
                cursor.close()
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self):
        """Explicit offline compatibility entry point; never used by startup."""
        from app.database.migrations import upgrade

        await upgrade(self.engine)

    async def close(self):
        await self.engine.dispose()
