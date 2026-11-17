"""Permit multiple credentials per customer while retaining the original table."""
import re
from datetime import datetime, timezone

async def upgrade(engine):
    if engine.dialect.name != 'sqlite':
        return
    async with engine.connect() as connection:
        row=(await connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='proxy_credentials'")).first()
        if not row or not re.search(r'UNIQUE\s*\(\s*server_id\s*,\s*user_id\s*\)',row[0],re.I):
            return
        schema=re.sub(r',\s*UNIQUE\s*\(\s*server_id\s*,\s*user_id\s*\)', '',row[0],flags=re.I)
        schema=re.sub(r'CREATE TABLE\s+"?proxy_credentials"?', 'CREATE TABLE proxy_credentials_multi',schema,count=1,flags=re.I)
        archive='proxy_credentials_before_multi_'+datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
        await connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
        await connection.exec_driver_sql('PRAGMA legacy_alter_table=ON')
        await connection.commit()
        try:
            async with connection.begin():
                await connection.exec_driver_sql('BEGIN IMMEDIATE')
                await connection.exec_driver_sql(schema)
                await connection.exec_driver_sql('INSERT INTO proxy_credentials_multi SELECT * FROM proxy_credentials')
                before=(await connection.exec_driver_sql('SELECT count(*) FROM proxy_credentials')).scalar()
                after=(await connection.exec_driver_sql('SELECT count(*) FROM proxy_credentials_multi')).scalar()
                if before!=after:raise RuntimeError('Proxy migration row count mismatch')
                await connection.exec_driver_sql('ALTER TABLE proxy_credentials RENAME TO '+archive)
                await connection.exec_driver_sql('ALTER TABLE proxy_credentials_multi RENAME TO proxy_credentials')
                await connection.exec_driver_sql('CREATE INDEX ix_proxy_multi_user ON proxy_credentials(user_id)')
                await connection.exec_driver_sql('CREATE INDEX ix_proxy_multi_server ON proxy_credentials(server_id)')
                violations=(await connection.exec_driver_sql('PRAGMA foreign_key_check')).all()
                if violations:raise RuntimeError('Proxy migration foreign key check failed')
        finally:
            await connection.exec_driver_sql('PRAGMA legacy_alter_table=OFF')
            await connection.exec_driver_sql('PRAGMA foreign_keys=ON')
            await connection.commit()
