"""Security boundaries: roles, cross-site writes, private files and identity I/O."""
import base64
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI
from starlette.responses import Response

from app.config.settings import Settings
from app.services.freeman_ai.files import decode
from app.web.access import require_business_admin
from app.web.customer_identity import IdentityMiddleware
from app.web.security import PublicStaticFiles, SecurityMiddleware, telegram_media_headers


class SecurityBoundaries(unittest.IsolatedAsyncioTestCase):
    async def test_static_cache_requires_matching_public_content_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = b'body{}'
            Path(tmp, 'style.css').write_bytes(body)
            Path(tmp, 'index.html').write_text('public page')
            app = FastAPI()
            app.mount('/assets', PublicStaticFiles(directory=tmp))
            version = hashlib.sha256(body).hexdigest()[:12]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://example.test') as client:
                good = await client.get('/assets/style.css?v=' + version)
                self.assertEqual(good.headers['Cache-Control'], 'public, max-age=31536000, immutable')
                cached = await client.get('/assets/style.css?v=' + version, headers={'If-None-Match': good.headers['etag']})
                self.assertEqual(cached.status_code, 304)
                self.assertIn('immutable', cached.headers['Cache-Control'])
                for url in ('style.css', 'style.css?v=old', 'style.css?v=' + version + '&v=old', 'index.html?v=' + version):
                    self.assertEqual((await client.get('/assets/' + url)).headers['Cache-Control'], 'no-cache')

    async def test_browser_origin_boundary_and_private_response_headers(self):
        app = FastAPI()
        app.add_middleware(SecurityMiddleware, web_base_url='https://example.test/')

        @app.api_route('/api/change', methods=['GET', 'POST', 'DELETE'])
        async def change():
            return Response('ok', headers={'Cache-Control': 'private, max-age=300'})

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://example.test') as client:
            for headers in ({'Origin': 'https://evil.test'}, {'Origin': 'null'}, {'Sec-Fetch-Site': 'cross-site'}):
                self.assertEqual((await client.post('/api/change', headers=headers)).status_code, 403)
            for headers in ({}, {'Origin': 'https://example.test', 'Sec-Fetch-Site': 'same-origin'}):
                result = await client.post('/api/change', headers=headers)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.headers['Cache-Control'], 'no-store')
                self.assertEqual(result.headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual((await client.delete('/api/change', headers={'Origin': 'https://evil.test'})).status_code, 403)

    async def test_hidden_metadata_is_not_a_public_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'style.css').write_text('body{}')
            Path(tmp, '._style.css').write_text('private metadata')
            Path(tmp, '.private').mkdir()
            Path(tmp, '.private', 'data').write_text('private')
            app = FastAPI()
            app.mount('/assets', PublicStaticFiles(directory=tmp))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://example.test') as client:
                self.assertEqual((await client.get('/assets/style.css')).status_code, 200)
                for name in ('._style.css', '.private/data'):
                    self.assertEqual((await client.get('/assets/' + name)).status_code, 404)

    async def test_staff_cannot_read_business_admin_apis_or_publish(self):
        from app.web.admin_dashboard import admin_dashboard_router
        from app.web.admin_payments import admin_payments_router
        from app.web.admin_reports import admin_reports_router
        from app.web.admin_proxy import admin_proxy_router
        from app.web.customer_profile import customer_profile_router
        from app.web.catalog import catalog_router
        from app.web.offers import offers_router
        from app.web.posts import posts_router
        from app.web.proxy_control import proxy_control_router

        settings = Settings(admin_ids='1', _env_file=None)
        settings.set_staff_roles({2: 'support', 3: 'manager', 4: 'administrator'})
        actor = SimpleNamespace(telegram_id=2)

        async def current(token):
            return actor

        app = FastAPI()
        for factory in (admin_dashboard_router, admin_payments_router, admin_reports_router,
                        admin_proxy_router, customer_profile_router, catalog_router,
                        offers_router, posts_router, proxy_control_router):
            # A denial must happen before opening a database connection.
            app.include_router(factory(None, settings, current))
        paths = ['/api/web/admin/dashboard', '/api/web/admin/users', '/api/web/admin/keys',
                 '/api/web/admin/payments', '/api/web/admin/reports', '/api/web/admin/proxy',
                 '/api/web/admin/customers/1/profile', '/api/web/admin/catalog',
                 '/api/web/admin/offers/context?key_id=1', '/api/web/admin/publications',
                 '/api/web/admin/proxy/control/settings', '/api/web/posts?admin_view=true']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://example.test') as client:
            for tid in (2, 3, 99):
                actor.telegram_id = tid
                for path in paths:
                    with self.subTest(role=tid, path=path):
                        self.assertEqual((await client.get(path)).status_code, 403)
                self.assertEqual((await client.post('/api/web/admin/publications', json={'title': 'Test'})).status_code, 403)
                self.assertEqual((await client.post('/api/web/admin/proxy/control/list', json={})).status_code, 403)
        for tid in (1, 4):
            actor.telegram_id = tid
            self.assertIs(require_business_admin(actor, settings), actor)
        self.assertTrue(settings.can_admin_action(2, 'admin:t:home'))
        self.assertTrue(settings.can_free_subscription(3))

    async def test_ordinary_api_responses_do_not_bind_trusted_browser(self):
        database = SimpleNamespace(sessions=Mock())

        async def endpoint(scope, receive, send):
            await Response('ok')(scope, receive, send)

        app = IdentityMiddleware(endpoint, database, SimpleNamespace())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://example.test', cookies={'freeman_session': 'existing'}) as client:
            self.assertEqual((await client.get('/api/web/profile')).status_code, 200)
        database.sessions.begin.assert_not_called()

    async def test_new_login_still_binds_trust(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def transaction():
            yield object()

        database = SimpleNamespace(sessions=SimpleNamespace(begin=transaction))

        async def endpoint(scope, receive, send):
            response = Response('ok')
            response.set_cookie('freeman_session', 'new-session')
            await response(scope, receive, send)

        app = IdentityMiddleware(endpoint, database, SimpleNamespace())
        with patch('app.web.customer_identity.identity.authenticated', new=AsyncMock(return_value=SimpleNamespace(id=1))) as authenticated, patch('app.web.customer_identity.identity.attach_trust', new=AsyncMock()) as bind:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://example.test') as client:
                self.assertEqual((await client.post('/auth/bot/code')).status_code, 200)
            self.assertEqual(authenticated.call_args.args[1].cookies['freeman_session'], 'new-session')
            bind.assert_awaited_once()

    async def test_telegram_documents_are_downloads_not_executable_pages(self):
        document = telegram_media_headers('document', 'documents/attack\r\n.html')
        self.assertTrue(document['Content-Disposition'].startswith('attachment;'))
        self.assertNotIn('\r', document['Content-Disposition'])
        self.assertIn('sandbox', document['Content-Security-Policy'])
        self.assertEqual(document['X-Content-Type-Options'], 'nosniff')
        self.assertNotIn('Content-Disposition', telegram_media_headers('photo', 'photos/a.jpg'))

    async def test_untrusted_docx_cannot_expand_xml_entities(self):
        xml = b'<!DOCTYPE a [<!ENTITY evil "EXPANDED_ENTITY">]><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p>&evil;</w:p></w:document>'
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('word/document.xml', xml)
        with self.assertRaises(ValueError):
            decode({'name': 'attack.docx', 'data': base64.b64encode(stream.getvalue()).decode()})
        ordinary = xml.split(b']>', 1)[1].replace(b'&evil;', 'Инструкция'.encode())
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('word/document.xml', ordinary)
        self.assertEqual(decode({'name': 'guide.docx', 'data': base64.b64encode(stream.getvalue()).decode()})['text'], 'Инструкция')

    async def test_document_excerpt_reports_truncation(self):
        result = decode({'name': 'guide.txt', 'data': base64.b64encode(b'x' * 7000).decode()})
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['text']), 6000)


class PrivateMediaIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_file_ownership_and_attachment_response(self):
        import json
        import time
        from cryptography.fernet import Fernet
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.database.models import Base, User, Ticket, TicketMessage
        from app.web.routes import website_router

        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions.begin() as session:
                user = User(telegram_id=1, freeman_id='FO-1')
                other = User(telegram_id=2, freeman_id='FO-2')
                session.add_all([user, other])
                await session.flush()
                ticket = Ticket(user_id=user.id)
                session.add(ticket)
                await session.flush()
                message = TicketMessage(ticket_id=ticket.id, role='client', text='', document='test-file', source='test:private-media')
                session.add(message)
                await session.flush()
                mid, uid, oid = message.id, user.id, other.id
            settings = Settings(admin_ids='999', encryption_key=Fernet.generate_key().decode(), _env_file=None)
            app = FastAPI()
            app.include_router(website_router(SimpleNamespace(sessions=sessions), settings))

            class FakeBot:
                calls = 0

                def __init__(self, token): pass
                async def __aenter__(self): return self
                async def __aexit__(self, *args): pass
                async def get_file(self, file_id):
                    FakeBot.calls += 1
                    return SimpleNamespace(file_size=15, file_path='documents/attack.html')
                async def download_file(self, path): return io.BytesIO(b'<script>evil()</script>')

            def token(id):
                return Fernet(settings.encryption_key.encode()).encrypt(json.dumps({'user_id': id, 'version': 0, 'expires': time.time() + 3600}).encode()).decode()

            with patch('aiogram.Bot', FakeBot):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://example.test') as client:
                    path = f'/api/web/support/messages/{mid}/media/document'
                    self.assertEqual((await client.get(path)).status_code, 401)
                    client.cookies.set('freeman_session', token(oid))
                    self.assertEqual((await client.get(path)).status_code, 404)
                    self.assertEqual(FakeBot.calls, 0)
                    client.cookies.set('freeman_session', token(uid))
                    result = await client.get(path)
                    self.assertEqual(result.status_code, 200)
                    self.assertTrue(result.headers['content-disposition'].startswith('attachment;'))
                    self.assertIn('sandbox', result.headers['content-security-policy'])
                    self.assertEqual(FakeBot.calls, 1)
        finally:
            await engine.dispose()
