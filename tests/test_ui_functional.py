"""UI query contracts on disposable PostgreSQL; no provider calls."""
import json
import os
import unittest
from datetime import timedelta
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from test_freeman_ai import Tests as DatabaseFixture

from app.config.settings import Settings
from app.database.models import (
    AccessKey, AppSetting, Device, RemnawaveServer, Subscription, User,
    VlessCredential, now,
)
from app.web.admin_dashboard import admin_dashboard_router


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "isolated PostgreSQL")
class UIFunctional(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = DatabaseFixture.asyncSetUp
    asyncTearDown = DatabaseFixture.asyncTearDown

    async def client(self):
        self.settings = Settings(_env_file=None, bot_token="", admin_ids="123", encryption_key=self.settings.encryption_key)
        async with self.db.sessions() as s:
            actor = await s.get(User, self.uid)

        async def current(_):
            return actor

        app = FastAPI()
        app.include_router(admin_dashboard_router(self.db, self.settings, current))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.test")
        self.addAsyncCleanup(client.aclose)
        return client

    async def seed_keys(self):
        async with self.db.sessions.begin() as s:
            panel = RemnawaveServer(name="Isolated", api_url="https://panel.example.test", api_token="unused", country="TEST")
            s.add(panel)
            await s.flush()
            self.ids = {}
            for label, count, capacity in [("unknown", None, 3), ("none", 0, 3), ("partial", 1, 3), ("full", 3, 3), ("over", 4, 3), ("disconnected", 0, 5)]:
                sub = Subscription(user_id=self.uid, kind="vpn", device_limit=capacity, expires_at=now()+timedelta(days=20))
                key = AccessKey(user_id=self.uid, name=label, mode="vless", expires_at=sub.expires_at)
                s.add_all([sub, key]); await s.flush()
                s.add(VlessCredential(user_id=self.uid, subscription_id=sub.id, access_key_id=key.id, server_id=panel.id, remote_user_uuid=str(key.id), subscription_url="https://sub.example.test/"+label, expires_at=sub.expires_at))
                self.ids[label] = key.id
                if count is not None:
                    s.add(AppSetting(key=f"vless_hwid_snapshot:{sub.id}", value=json.dumps({"last_successful_at": now().isoformat()})))
                    for i in range(count):
                        s.add(Device(subscription_id=sub.id, name="Device", type="phone", provisioning_id=f"{label}-{i}", status="active"))
                if label == "disconnected":
                    s.add(Device(subscription_id=sub.id, name="Old", type="phone", provisioning_id="removed", status="disconnected"))

    async def test_device_filters_use_known_observations_and_paginate(self):
        await self.seed_keys(); client = await self.client()
        expected = {"none": {"none", "disconnected"}, "available": {"none", "partial", "disconnected"}, "partial": {"partial"}, "limit": {"full", "over"}, "unknown": {"unknown"}, "connected": {"partial", "full", "over"}}
        with patch("app.services.key_device_counts.device_counts", side_effect=AssertionError("Registry must not fan out to providers")):
            for value, names in expected.items():
                r = await client.post("/api/web/admin/keys/search", json={"devices": value, "page": 100, "limit": 10})
                self.assertEqual(r.status_code, 200, r.text)
                data = r.json(); self.assertEqual({k["name"] for k in data["items"]}, names)
                self.assertEqual(data["total"], len(names)); self.assertEqual(data["page"], 1)
                for k in data["items"]:
                    self.assertEqual(k["device_count"] is None, k["name"] == "unknown")
            data = (await client.post("/api/web/admin/keys/search", json={"device_limit": 5})).json()
            self.assertEqual([k["name"] for k in data["items"]], ["disconnected"])
            self.assertIn(5, data["device_limits"])

    async def test_client_page_size_counts_and_pagination(self):
        client = await self.client()
        async with self.db.sessions.begin() as s:
            s.add_all(User(first_name=f"Client {i}", freeman_id=f"FO-{i+1000}") for i in range(53))
        for size in [10, 25, 50, 100]:
            r = await client.post("/api/web/admin/users/search", json={"limit": size, "page": 1})
            self.assertEqual(r.status_code, 200, r.text); data = r.json()
            self.assertEqual(data["total"], 55); self.assertEqual(data["limit"], size)
            self.assertEqual(len(data["items"]), min(size, 55))
            r = await client.post("/api/web/admin/users/search", json={"limit": size, "page": 2})
            data = r.json(); self.assertEqual(data["page"], min(2, (55+size-1)//size))
