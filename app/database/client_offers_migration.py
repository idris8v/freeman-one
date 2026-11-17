"""Backward-compatible nullable CRM offers and redemption counters."""
from sqlalchemy import inspect
async def upgrade(engine):
 async with engine.begin() as c:
  tables=await c.run_sync(lambda s:inspect(s).get_table_names())
  if 'personal_offers' not in tables:return
  columns=await c.run_sync(lambda s:inspect(s).get_columns('personal_offers'));names={r['name'] for r in columns}
  if 'usage_limit' not in names:await c.exec_driver_sql('ALTER TABLE personal_offers ADD COLUMN usage_limit INTEGER DEFAULT 1')
  if 'use_count' not in names:await c.exec_driver_sql('ALTER TABLE personal_offers ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0')
  if 'no_stack' not in names:await c.exec_driver_sql('ALTER TABLE personal_offers ADD COLUMN no_stack BOOLEAN NOT NULL DEFAULT TRUE')
  await c.exec_driver_sql("UPDATE personal_offers SET use_count=1 WHERE status='used' AND use_count=0")
  if c.dialect.name=='postgresql':
   await c.exec_driver_sql('ALTER TABLE personal_offers ALTER COLUMN key_id DROP NOT NULL')
   await c.exec_driver_sql('ALTER TABLE personal_offers ALTER COLUMN subscription_id DROP NOT NULL')
 # SQLite production is no longer used; support existing local databases without
 # changing IDs or foreign-key references via a transactional table rebuild.
 if engine.dialect.name=='sqlite' and any(not r['nullable'] for r in columns if r['name'] in ('key_id','subscription_id')):
  from sqlalchemy.schema import CreateTable,CreateIndex
  from app.database.models import PersonalOffer
  async with engine.connect() as c:
   await c.exec_driver_sql('PRAGMA foreign_keys=OFF');await c.commit()
   try:
    await c.exec_driver_sql('BEGIN IMMEDIATE')
    ddl=str(CreateTable(PersonalOffer.__table__).compile(dialect=c.dialect)).replace('CREATE TABLE personal_offers','CREATE TABLE personal_offers_crm_upgrade',1)
    await c.exec_driver_sql(ddl)
    names=[col.name for col in PersonalOffer.__table__.columns];quoted=','.join('"'+n+'"' for n in names)
    await c.exec_driver_sql(f'INSERT INTO personal_offers_crm_upgrade ({quoted}) SELECT {quoted} FROM personal_offers')
    await c.exec_driver_sql('DROP TABLE personal_offers');await c.exec_driver_sql('ALTER TABLE personal_offers_crm_upgrade RENAME TO personal_offers')
    for ix in PersonalOffer.__table__.indexes:await c.exec_driver_sql(str(CreateIndex(ix).compile(dialect=c.dialect)))
    await c.commit()
   except Exception:await c.rollback();raise
   finally:await c.exec_driver_sql('PRAGMA foreign_keys=ON');await c.commit()
