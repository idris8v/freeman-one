"""Consistent local SQLite backups. Run once daily via systemd timer."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlite3
import os,subprocess
from sqlalchemy.engine import make_url
from app.config.settings import Settings


def backup():
    settings = Settings()
    if settings.database_url.startswith('postgresql'):
        url=make_url(settings.database_url)
        directory=Path('/app/data/backups');directory.mkdir(parents=True,exist_ok=True)
        stamp=datetime.now(timezone.utc);target=directory/f'freeman-{stamp:%Y%m%d-%H%M%S-%f}.pgdump';partial=target.with_suffix('.partial')
        env={**os.environ,'PGPASSWORD':url.password or ''}
        command=['pg_dump','--host',url.host or 'postgres','--port',str(url.port or 5432),'--username',url.username or 'freeman','--dbname',url.database,'--format=custom','--no-owner','--no-acl','--file',str(partial)]
        try:
            result=subprocess.run(command,env=env,capture_output=True,timeout=300)
            if result.returncode:raise RuntimeError('PostgreSQL backup failed; original database untouched')
            with partial.open('rb') as handle:magic=handle.read(5)
            if magic!=b'PGDMP':raise RuntimeError('Invalid PostgreSQL backup format')
            if subprocess.run(['pg_restore','--list',str(partial)],capture_output=True).returncode:raise RuntimeError('PostgreSQL backup catalog invalid')
            partial.replace(target)
        finally:partial.unlink(missing_ok=True)
        for file in directory.glob('freeman-*.pgdump'):
            if datetime.fromtimestamp(file.stat().st_mtime,timezone.utc)<stamp-timedelta(days=7):file.unlink()
        print(f'Backup created: {target.name}')
        return target
    if not settings.database_url.startswith('sqlite+aiosqlite:///'):
        raise RuntimeError('Unsupported database for backups')
    database = Path(settings.database_url.split('///', 1)[1])
    if not database.is_file():
        raise RuntimeError('Database does not exist')
    directory = database.parent / 'backups'
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    target = directory / f'freeman-{stamp:%Y%m%d-%H%M%S-%f}.db'
    with sqlite3.connect(f'file:{database.resolve().as_posix()}?mode=ro', uri=True) as source:
        with sqlite3.connect(target) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed')
    for file in directory.glob('freeman-*.db'):
        if datetime.fromtimestamp(file.stat().st_mtime, timezone.utc) < stamp - timedelta(days=7):
            file.unlink()
    print(f'Backup created: {target.name}')
    return target


if __name__ == '__main__':
    backup()
