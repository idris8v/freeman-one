"""Canonical publication navigation; isolated DB and fake Telegram transport."""

import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from test_freeman_ai import Tests as DatabaseFixture

from app.config.settings import Settings
from app.database.models import Campaign, Publication, WebNews, WebUsefulLink, now
from app.database.repositories import SQLRepository
from app.services.dispatch import DispatchWorker
from app.services.publication_urls import legacy_section_target, publication_url
from app.web.cabinet.context import WebContext
from app.web.campaign_post import router as campaign_post_router
from app.web.posts import load_json, migrate_publications, publication_tick
from app.web.publication_content import render_content
from app.web.routes import website_router
from app.web.security import SecurityMiddleware


class PublicationURLContract(unittest.TestCase):
    def test_configured_origin_port_path_and_trailing_slashes(self):
        for base, expected in (
            ("https://public.example", "https://public.example/?post=42"),
            ("https://public.example/", "https://public.example/?post=42"),
            ("https://other.example:9443", "https://other.example:9443/?post=42"),
            (
                "https://public.example/cabinet",
                "https://public.example/cabinet/?post=42",
            ),
            ("https://public.example/a/b///", "https://public.example/a/b/?post=42"),
            ("https://localhost:18443", "https://localhost:18443/?post=42"),
        ):
            with self.subTest(base=base):
                settings = SimpleNamespace(web_base_url=base)
                self.assertEqual(publication_url(settings, 42), expected)
                self.assertEqual(settings.web_base_url, base)

    def test_invalid_configuration_never_falls_back_to_request_or_default_host(self):
        for base in (
            "http://public.example",
            "https://",
            "https:///missing",
            "//evil.example",
            "https://user@public.example",
            "https://public.example@evil.example",
            "https://public.example/?next=https://evil.example",
            "https://public.example/#evil",
            "https://public.example\\@evil.example",
            "https://public.example:bad",
            "https://public.example:99999",
            "https://public.example/\npath",
        ):
            with self.subTest(base=base), self.assertRaises(ValueError):
                publication_url(SimpleNamespace(web_base_url=base), 42)

    def test_id_cannot_inject_path_query_or_redirect(self):
        for value in (
            None,
            True,
            0,
            -1,
            "//evil.example",
            "1&next=https://evil.example",
            "1#evil",
            "../1",
            "١",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                publication_url(
                    SimpleNamespace(web_base_url="https://public.example"), value
                )

    def test_only_known_generated_legacy_section_targets_are_rebased(self):
        settings = SimpleNamespace(web_base_url="https://public.example/app/")
        old = Settings.model_fields["web_base_url"].default
        for target in ("faq", old + "/?section=faq"):
            self.assertEqual(
                legacy_section_target(settings, target),
                "https://public.example/app/?section=faq",
            )
        for target in (
            "https://docs.example/faq",
            old + "/?section=faq&next=",
            old + "/?section=faq&section=keys",
            old + "/?section=unknown",
            "https://[broken",
            "javascript:alert(1)",
        ):
            self.assertEqual(legacy_section_target(settings, target), target)


class PublicationURLs(unittest.IsolatedAsyncioTestCase):
    asyncSetUpDatabase = DatabaseFixture.asyncSetUp
    asyncTearDownDatabase = DatabaseFixture.asyncTearDown

    async def asyncSetUp(self):
        await self.asyncSetUpDatabase()
        self.settings = Settings(
            _env_file=None,
            encryption_key=self.settings.encryption_key,
            bot_token="",
            admin_ids="123",
            web_base_url="https://public.example/cabinet/",
        )
        self.context = WebContext(self.db, self.settings)
        app = FastAPI()
        app.include_router(website_router(self.db, self.settings))
        app.include_router(campaign_post_router(self.db))
        app.add_middleware(SecurityMiddleware, web_base_url=self.settings.web_base_url)
        # A reverse proxy's internal request address is not a public URL source.
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://internal.invalid"
        )
        self.login(self.uid)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.asyncTearDownDatabase()

    async def test_public_reading_surfaces_share_theme_and_escape_content(self):
        async with self.db.sessions.begin() as session:
            await SQLRepository(session).set_setting(
                "campaign_post:fixture-public-ui",
                json.dumps(
                    {
                        "title": "<b>Title</b>",
                        "text": "</section><script>alert(1)</script>",
                    }
                ),
            )
        post = await self.client.get("/broadcast-post/fixture-public-ui")
        self.assertEqual(post.status_code, 200)
        self.assertIn('class="fo-card fo-scope fo-reading"', post.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", post.text)
        self.assertNotIn("<style>", post.text)
        self.assertIn("style-src 'self'", post.headers["content-security-policy"])
        for path in ("/documents", "/documents/0"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn('class="fo-card fo-scope fo-reading"', response.text)
            self.assertIn("/assets/account-management.css?v=", response.text)

    def login(self, uid):
        self.client.cookies.set("freeman_session", self.context.session_token(uid))

    async def stored(self):
        async with self.db.sessions() as s:
            return {
                "posts": [
                    (
                        p.id,
                        p.content,
                        p.body,
                        p.plain,
                        p.version,
                        p.updated_at,
                        p.link_id,
                    )
                    for p in await s.scalars(
                        select(Publication).order_by(Publication.id)
                    )
                ],
                "news": [
                    (n.id, n.title, n.text, n.target)
                    for n in await s.scalars(select(WebNews).order_by(WebNews.id))
                ],
                "links": [
                    (n.id, n.label, n.target)
                    for n in await s.scalars(
                        select(WebUsefulLink).order_by(WebUsefulLink.id)
                    )
                ],
            }

    async def existing(self, section="links", authored=False):
        old = Settings.model_fields["web_base_url"].default
        document = {
            "ops": [
                {"insert": "Existing article\n"},
                {
                    "insert": {
                        "button": {
                            "text": "Open instructions",
                            "url": old + "/?section=instructions",
                        }
                    }
                },
                {"insert": "\n"},
            ]
        }
        content, body, plain = render_content(document, [])
        async with self.db.sessions.begin() as s:
            n = WebNews(title="Existing", text=plain, category=section, published=True)
            s.add(n)
            await s.flush()
            n.target = old + "/?post=" + str(n.id)
            link = WebUsefulLink(
                label="Existing", target=n.target, icon="file-text", enabled=True
            )
            s.add(link)
            await s.flush()
            p = Publication(
                id=n.id,
                section=section,
                status="published",
                content=json.dumps(content),
                body=body,
                plain=plain,
                media="[]",
                link_id=link.id if section == "links" else None,
                created_by=self.uid if authored else None,
                notified_at=now(),
                published_at=now(),
                version=7,
            )
            s.add(p)
            return n.id, link.id

    async def test_original_legacy_migration_uses_configured_urls_and_retains_ids(self):
        async with self.db.sessions.begin() as s:
            news = WebNews(
                title="Legacy news", text="News", target="faq", published=True
            )
            link = WebUsefulLink(
                label="Instructions", target="instructions", icon="book", enabled=True
            )
            s.add_all([news, link])
            await s.flush()
            news_id, link_id = news.id, link.id
        await migrate_publications(self.db, self.settings)
        async with self.db.sessions() as s:
            n = await s.get(WebNews, news_id)
            self.assertEqual(n.target, publication_url(self.settings, news_id))
            p = await s.get(Publication, news_id)
            self.assertIn("https://public.example/cabinet/?section=faq", p.body)
            linked = await s.scalar(
                select(Publication).where(Publication.link_id == link_id)
            )
            self.assertIn(
                "https://public.example/cabinet/?section=instructions", linked.body
            )
            self.assertEqual(
                (await s.get(WebUsefulLink, link_id)).target,
                publication_url(self.settings, linked.id),
            )
        snapshot = await self.stored()
        await migrate_publications(self.db, self.settings)
        self.assertEqual(snapshot, await self.stored())

    async def test_existing_generated_links_and_buttons_are_corrected_without_data_rewrite(
        self,
    ):
        pid, lid = await self.existing()
        nid, _ = await self.existing(section="news")
        before = await self.stored()
        me = (await self.client.get("/api/web/me")).json()
        self.assertEqual(
            next(x for x in me["useful_links"] if x["id"] == lid)["target"],
            publication_url(self.settings, pid),
        )
        self.assertEqual(
            next(x for x in me["news"] if x["id"] == nid)["target"],
            publication_url(self.settings, nid),
        )
        full = (await self.client.get(f"/api/web/posts/{pid}")).json()
        self.assertEqual(full["url"], publication_url(self.settings, pid))
        self.assertIn(
            'href="https://public.example/cabinet/?section=instructions"', full["body"]
        )
        self.assertEqual(full["version"], 7)
        self.assertEqual(before, await self.stored())

    async def test_authored_content_and_external_links_remain_unchanged(self):
        pid, _ = await self.existing(authored=True)
        before = await self.stored()
        full = (await self.client.get(f"/api/web/posts/{pid}")).json()
        self.assertEqual(full["body"], before["posts"][0][2])
        self.assertEqual(json.loads(before["posts"][0][1]), full["content"])
        self.assertEqual(before, await self.stored())

    async def draft(self, **extra):
        response = await self.client.post(
            "/api/web/admin/publications",
            json={
                "title": "Canonical publication",
                "section": "links",
                "notify_clients": False,
                "content": {"ops": [{"insert": "Article text\n"}]},
                **extra,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_admin_save_publish_edit_and_customer_read_use_same_url(self):
        draft = await self.draft(url="https://evil.example/redirect")
        pid = draft["id"]
        self.assertEqual(draft["url"], publication_url(self.settings, pid))
        published = await self.client.post(
            f"/api/web/admin/publications/{pid}/action",
            json={"action": "publish", "version": draft["version"]},
        )
        self.assertEqual(published.status_code, 200, published.text)
        edited = await self.client.put(
            f"/api/web/admin/publications/{pid}",
            json={
                "title": "Updated",
                "section": "links",
                "content": draft["content"],
                "notify_clients": False,
                "version": published.json()["version"],
            },
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()["id"], pid)
        admin = (await self.client.get("/api/web/admin/publications")).json()
        self.assertEqual(admin["items"][0]["url"], draft["url"])
        self.login(self.other)
        for endpoint in ("/api/web/posts", f"/api/web/posts/{pid}"):
            data = (await self.client.get(endpoint)).json()
            self.assertEqual(
                (data[0] if isinstance(data, list) else data)["url"], draft["url"]
            )
        me = (await self.client.get("/api/web/me")).json()
        self.assertEqual(me["useful_links"][0]["target"], draft["url"])

    async def test_request_host_forwarded_headers_and_extra_parameters_cannot_choose_origin(
        self,
    ):
        pid, _ = await self.existing()
        response = await self.client.get(
            f"/api/web/posts/{pid}?next=https://evil.example",
            headers={
                "Host": "127.0.0.1:8000",
                "X-Forwarded-Host": "evil.example",
                "X-Forwarded-Proto": "http",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], publication_url(self.settings, pid))
        self.assertNotIn("location", response.headers)

    async def test_publication_authorization_and_url_validation_remain_enforced(self):
        draft = await self.draft()
        unsafe = await self.client.post(
            "/api/web/admin/publications",
            json={
                "title": "Unsafe",
                "content": {
                    "ops": [
                        {
                            "insert": {
                                "button": {"text": "Go", "url": "javascript:alert(1)"}
                            }
                        }
                    ]
                },
            },
        )
        self.assertEqual(unsafe.status_code, 422)
        self.login(self.other)
        self.assertEqual(
            (await self.client.get(f"/api/web/posts/{draft['id']}")).status_code, 404
        )
        self.assertEqual(
            (await self.client.get("/api/web/admin/publications")).status_code, 403
        )
        self.client.cookies.clear()
        self.assertEqual(
            (await self.client.get(f"/api/web/posts/{draft['id']}")).status_code, 401
        )

    async def test_scheduled_publication_uses_configured_origin_and_existing_outbox(
        self,
    ):
        future = now() + timedelta(hours=1)
        draft = await self.draft(notify_clients=True, scheduled_at=future.isoformat())
        result = await self.client.post(
            f"/api/web/admin/publications/{draft['id']}/action",
            json={"action": "publish", "version": draft["version"]},
        )
        self.assertEqual(result.json()["status"], "scheduled")
        with patch("app.web.posts.now", return_value=future + timedelta(seconds=1)):
            await publication_tick(self.db, self.settings)
            await publication_tick(self.db, self.settings)
        async with self.db.sessions() as s:
            p = await s.get(Publication, draft["id"])
            self.assertEqual(p.status, "published")
            self.assertEqual(
                (await s.get(WebNews, p.id)).target,
                publication_url(self.settings, p.id),
            )
            self.assertEqual(len(list(await s.scalars(select(Campaign)))), 1)
            snapshot = await load_json(
                SQLRepository(s), f"web_post_delivery:{p.campaign_id}"
            )
            self.assertEqual(snapshot["id"], p.id)

    async def test_telegram_notification_retries_use_configured_origin_and_original_id(
        self,
    ):
        bot = SimpleNamespace(send_message=AsyncMock())
        worker = DispatchWorker(self.db, bot, self.settings)
        snapshot = {
            "id": 42,
            "title": "Stored title",
            "body": "Stored body",
            "media": [],
            "url": "https://evil.example/ignored",
        }
        await worker.send_post(900, 123, snapshot, "Stored body")
        await worker.send_post(900, 123, snapshot, "Stored body")
        bot.send_message.assert_awaited_once()
        self.assertEqual(
            bot.send_message.await_args.kwargs["reply_markup"]
            .inline_keyboard[0][0]
            .url,
            publication_url(self.settings, 42),
        )
        self.assertEqual(snapshot["id"], 42)
