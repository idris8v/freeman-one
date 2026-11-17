"""Idempotent upgrade of the existing customer table; IDs and foreign keys survive."""
from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateTable, CreateIndex
from app.database.models import User, ReferralReward


async def upgrade(engine):
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
        if 'users' not in tables:
            return
        columns = await connection.run_sync(lambda conn: inspect(conn).get_columns('users'))
        if connection.dialect.name == 'sqlite':
            # Rebuild only users: SQLite cannot make an existing column nullable.
            names = {column['name'] for column in columns}
            needs_rebuild = not next(c for c in columns if c['name']=='telegram_id')['nullable']
            if needs_rebuild:
                await connection.commit()
                await connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
                await connection.exec_driver_sql('BEGIN IMMEDIATE')
                ddl = str(CreateTable(User.__table__).compile(dialect=connection.dialect))
                await connection.exec_driver_sql(ddl.replace('CREATE TABLE users', 'CREATE TABLE users_account_upgrade', 1))
                defaults = {'email_verified':'0','registration_source':"'telegram'",'auth_version':'0','updated_at':'created_at'}
                targets = [c.name for c in User.__table__.columns]
                expressions = [f'"{name}"' if name in names else defaults.get(name,'NULL') for name in targets]
                await connection.exec_driver_sql('INSERT INTO users_account_upgrade (' + ','.join(f'"{n}"' for n in targets) + ') SELECT ' + ','.join(expressions) + ' FROM users')
                await connection.exec_driver_sql('DROP TABLE users')
                await connection.exec_driver_sql('ALTER TABLE users_account_upgrade RENAME TO users')
                for index in User.__table__.indexes:
                    await connection.exec_driver_sql(str(CreateIndex(index).compile(dialect=connection.dialect)))
                await connection.commit()
                await connection.exec_driver_sql('PRAGMA foreign_keys=ON')
            else:
                await add_columns(connection, User, names)
        else:
            await connection.exec_driver_sql('ALTER TABLE users ALTER COLUMN telegram_id DROP NOT NULL')
            await add_columns(connection, User, {c['name'] for c in columns})
        if 'referral_rewards' in tables:
            existing = await connection.run_sync(lambda conn: {c['name'] for c in inspect(conn).get_columns('referral_rewards')})
            await add_columns(connection, ReferralReward, existing)
        rows = (await connection.execute(text('SELECT id, freeman_id FROM users'))).all()
        for user_id, freeman_id in rows:
            if not freeman_id:
                old = None
                if 'app_settings' in tables:
                    old = await connection.scalar(text('SELECT value FROM app_settings WHERE key=:key'), {'key':f'freeman_id:{user_id}'})
                await connection.execute(text('UPDATE users SET freeman_id=:fid WHERE id=:id'), {'fid':old or f'FRM-{user_id:06d}', 'id':user_id})
        # Preserve legacy addresses without treating them as verified identities.
        if 'app_settings' in tables:
            from app.services.accounts import normalize_email
            legacy=(await connection.execute(text("SELECT key,value FROM app_settings WHERE key LIKE 'user_email:%'"))).all()
            for key,value in legacy:
                try: email=normalize_email(value);user_id=int(key.split(':')[1])
                except (ValueError,IndexError): continue
                owner=await connection.scalar(text('SELECT id FROM users WHERE email=:email'),{'email':email})
                if not owner:
                    await connection.execute(text('UPDATE users SET email=:email WHERE id=:id AND email IS NULL'),{'email':email,'id':user_id})
        for field in ('freeman_id','account_username','email'):
            await connection.exec_driver_sql(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_users_{field} ON users ({field})')
        await connection.commit()
        if connection.dialect.name == 'sqlite':
            violations = (await connection.exec_driver_sql('PRAGMA foreign_key_check')).all()
            if violations:
                raise RuntimeError('Account migration found invalid foreign keys')


async def add_columns(connection, model, existing):
    defaults = {'email_verified':'FALSE','registration_source':"'telegram'",'auth_version':'0',
        'payment_amount':'0','percentage':'0','tier':'1','purchase_type':"'legacy'",'status':"'credited'"}
    for column in model.__table__.columns:
        if column.name in existing:
            continue
        column_type = column.type.compile(dialect=connection.dialect)
        default = f' DEFAULT {defaults[column.name]}' if column.name in defaults else ''
        await connection.exec_driver_sql(f'ALTER TABLE {model.__tablename__} ADD COLUMN {column.name} {column_type}{default}')
