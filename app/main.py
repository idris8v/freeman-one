import asyncio
import logging
from contextlib import asynccontextmanager, suppress

import uvicorn
from aiogram import Bot
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config.settings import Settings
from app.database.repositories import SQLRepository
from app.database.schema_readiness import SchemaLease, SchemaReadinessMiddleware
from app.database.session import Database
from app.services.domain import seed


def create_app(settings=None):
    settings = settings or Settings()
    database = Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        bot_runtime = None
        tasks = []
        monitor = None
        lease = database.schema_lease = SchemaLease(database)

        def spawn(factory):
            task = asyncio.create_task(lease.run_component(factory))
            tasks.append(task)
            return task

        async def stop_work():
            for task in tasks:
                task.cancel()
            if bot_runtime:
                await bot_runtime.close()
            await asyncio.gather(*tasks, return_exceptions=True)

        async def watch_schema():
            while True:
                await asyncio.sleep(5)
                if not await lease.ping():
                    logging.getLogger(__name__).error("Schema lease lost; stopping application work")
                    await stop_work()
                    return

        try:
            state = await lease.open()
            if not state.ready:
                logging.getLogger(__name__).error("Database NOT READY: %s; run the offline migration command", state.reason)
                # HTTP stays observable, but no seed, bot, jobs or customer work.
                yield
                return
            async with database.sessions.begin() as session:
                repo=SQLRepository(session)
                await seed(repo, settings)
                from app.services.payment_settings import PaymentSettings
                await PaymentSettings(repo, settings).hydrate()
                roles=await repo.settings_prefix('staff_role:')
                settings.set_staff_roles({int(key.split(':',1)[1]):value for key,value in roles.items() if key.split(':',1)[1].isdigit() and await repo.setting('staff_blocked:'+key.split(':',1)[1])!='1'})
                from app.services.gifts import cancel_legacy_notifications
                await cancel_legacy_notifications(session)
            from app.web.posts import migrate_publications, publication_worker
            await migrate_publications(database, settings)
            spawn(lambda: publication_worker(database, settings))
            from app.services.customer_controls import worker as customer_worker
            spawn(lambda: customer_worker(database,settings))
            from app.services.email_delivery import EmailWorker, prepare_mail
            async with database.sessions.begin() as session:
                await prepare_mail(SQLRepository(session))
            spawn(lambda: EmailWorker(database, settings).run())
            # VPN/Proxy recovery is independent of Telegram identity and polling.
            from app.services.proxies import ProxyServerWorker
            app.state.proxy_worker_task = spawn(lambda: ProxyServerWorker(database, settings).run())
            from app.services.vpn import VpnWorker
            app.state.vpn_worker_task = spawn(lambda: VpnWorker(database, settings).run())
            from app.services.remnawave import RemnawaveWorker
            app.state.remnawave_worker_task = spawn(lambda: RemnawaveWorker(database, settings).run())
            from app.bot.runtime import BotRuntime
            app.state.polling_task = app.state.worker_task = app.state.backup_worker_task = None
            bot_runtime = BotRuntime(database, settings, app.state, bot_factory=Bot)
            app.state.bot_runtime = bot_runtime
            bot_runtime.start()
            if settings.freeman_ai_enabled or getattr(settings,'freeman_customer_ai_enabled',False):
                from app.services.freeman_ai.worker import worker as ai_worker
                spawn(lambda: ai_worker(database,settings))
            monitor = asyncio.create_task(watch_schema(), name="schema-readiness")
            yield
        finally:
            if monitor:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
            try:
                await stop_work()
            finally:
                try:
                    await lease.close()
                finally:
                    from app.services.panel_transport import close as close_panels
                    try:await close_panels()
                    finally:await database.close()

    app = FastAPI(title='F R E E M A N', lifespan=lifespan)

    from app.web.routes import STATIC, website_router
    from app.web.security import PublicStaticFiles, SecurityMiddleware
    app.mount('/assets', PublicStaticFiles(directory=STATIC), name='website-assets')
    from app.web.bot_checkout import router as bot_checkout_router
    app.include_router(bot_checkout_router(database,settings))
    from app.services.paid_install_support import router as install_help_router
    app.include_router(install_help_router(database, settings))
    app.include_router(website_router(database, settings))
    from app.web.campaign_post import router as campaign_post_router
    app.include_router(campaign_post_router(database))
    from app.web.customer_identity import IdentityMiddleware
    app.add_middleware(IdentityMiddleware,database=database,settings=settings)
    app.add_middleware(SchemaReadinessMiddleware, database=database)
    app.add_middleware(SecurityMiddleware, web_base_url=settings.web_base_url)

    @app.get('/health')
    async def health():
        try:
            async with database.sessions() as session:
                await session.execute(text('SELECT 1'))
        except Exception:
            return JSONResponse({'status': 'unhealthy', 'database': 'unavailable'}, status_code=503)
        runtime = getattr(app.state, 'bot_runtime', None)
        bot_status = runtime.health() if runtime else {'state': 'starting' if settings.bot_token else 'token_missing'}
        lease = getattr(database, 'schema_lease', None)
        if lease is None or not await lease.ping():
            return JSONResponse({'status': 'NOT READY', 'database': 'ok',
                'schema': lease.state.as_dict() if lease else {'status': 'NOT READY', 'reason': 'starting'},
                'bot': 'not_started_schema'}, status_code=503)
        return {'status': 'ok', 'database': 'ok', 'schema': lease.state.as_dict(), 'bot': bot_status['state'], 'bot_health': bot_status}

    @app.get('/live')
    async def live():
        return {'status': 'alive'}

    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    # Avoid request-level logs that might expose Telegram token-bearing URLs.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('aiohttp').setLevel(logging.WARNING)
    uvicorn.run(create_app(), host='0.0.0.0', port=8000, access_log=False)

