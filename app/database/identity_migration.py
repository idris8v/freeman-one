"""Add recognition without altering historical customer ownership."""
from sqlalchemy import inspect
from app.database.models import TrustedCustomerDevice,GuestPurchaseContext
async def upgrade(engine):
    async with engine.begin() as c:
        if 'users' not in await c.run_sync(lambda conn:inspect(conn).get_table_names()):return
        await c.run_sync(lambda conn: GuestPurchaseContext.__table__.create(conn,checkfirst=True))
        await c.run_sync(lambda conn: TrustedCustomerDevice.__table__.create(conn,checkfirst=True))
        tables=await c.run_sync(lambda conn:inspect(conn).get_table_names())
        if 'guest_orders' not in tables:return
        names=await c.run_sync(lambda conn:{col['name'] for col in inspect(conn).get_columns('guest_orders')})
        if engine.dialect.name=='postgresql':await c.exec_driver_sql('ALTER TABLE guest_orders ALTER COLUMN user_id DROP NOT NULL')
        elif not next(x for x in await c.run_sync(lambda conn:inspect(conn).get_columns('guest_orders')) if x['name']=='user_id')['nullable']:
            raise RuntimeError('Trusted checkout upgrade requires PostgreSQL or a fresh SQLite database')
        if 'checkout_id' not in names:await c.exec_driver_sql('ALTER TABLE guest_orders ADD COLUMN checkout_id INTEGER REFERENCES guest_purchase_contexts(id)')
        if 'verified_email' not in names:await c.exec_driver_sql('ALTER TABLE guest_orders ADD COLUMN verified_email VARCHAR(254)')
        await c.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS uq_guest_payment_identity ON guest_orders(payment_id) WHERE checkout_id IS NOT NULL')
