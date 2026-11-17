"""I06 PostgreSQL HWID synchronization; all provider requests are mocked."""

import asyncio
import copy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import delete, select
from test_billing_concurrency import BillingConcurrency

from app.database.models import Device, Subscription, VlessCredential
from app.database.repositories import SQLRepository
from app.services.remnawave import RemnawaveApiError
from app.services.vless_hwid import (
    cache_state,
    confirmed_removal,
    parse_devices,
    synchronize,
)
from app.web.admin_dashboard import admin_dashboard_router
from app.web.cabinet.keys import keys_router


def hwid(name="a", **fields):
    return dict(
        hwid=name,
        userId=123,
        platform="iOS",
        osVersion=None,
        deviceModel="iPhone",
        userAgent=None,
        requestIp=None,
        createdAt="2026-10-01T12:00:00Z",
        updatedAt="2026-10-02T12:00:00Z",
        **fields,
    )


def snapshot(*items):
    return {"response": {"total": len(items), "devices": list(items)}}


MALFORMED = {
    "missing_devices": {"response": {"total": 0}},
    "missing_total": {"response": {"devices": []}},
    "missing_envelope": {"total": 0, "devices": []},
    "null_devices": {"response": {"total": 0, "devices": None}},
    "string_devices": {"response": {"total": 0, "devices": "[]"}},
    "partial_list": {"response": {"total": 2, "devices": [hwid()]}},
    "partial_empty": {"response": {"total": 2, "devices": []}},
    "wrong_total_type": {"response": {"total": False, "devices": []}},
    "invalid_entry": snapshot(hwid(), None),
    "missing_hwid": snapshot({k: v for k, v in hwid().items() if k != "hwid"}),
    "empty_hwid": snapshot({**hwid(), "hwid": ""}),
    "duplicate_hwid": snapshot(hwid(), hwid()),
    "wrong_user": snapshot({**hwid(), "userId": 456}),
    "missing_metadata": snapshot(
        {k: v for k, v in hwid().items() if k != "deviceModel"}
    ),
    "malformed_metadata": snapshot({**hwid(), "platform": 42}),
    "bad_timestamp": snapshot({**hwid(), "updatedAt": "yesterday"}),
    "missing_timestamp": snapshot(
        {k: v for k, v in hwid().items() if k != "createdAt"}
    ),
    "naive_timestamp": snapshot({**hwid(), "createdAt": "2026-10-01T12:00:00"}),
}


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class VlessDevices(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await BillingConcurrency.asyncSetUp(self)
        self.sid, self.kid, self.expiry = await BillingConcurrency.subscription(self)
        async with self.db.sessions.begin() as session:
            credential = await session.scalar(select(VlessCredential))
            credential.remote_user_uuid = "123"
            self.cid = credential.id
            session.add_all(
                [
                    Device(
                        subscription_id=self.sid,
                        name=h,
                        type="phone",
                        status="active",
                        provisioning_id=h,
                    )
                    for h in ("a", "b")
                ]
            )
        self.api = AsyncMock()
        self.api.devices.return_value = {"response": {}}
        api_patch = patch(
            "app.services.remnawave.RemnawaveService.api", return_value=self.api
        )
        api_patch.start()
        self.addCleanup(api_patch.stop)

        async def current_user(_):
            return self.user

        app = FastAPI()
        app.include_router(admin_dashboard_router(self.db, self.settings, current_user))
        app.include_router(
            keys_router(
                SimpleNamespace(
                    database=self.db, settings=self.settings, current_user=current_user
                )
            )
        )
        self.web = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://example.test"
        )
        self.addAsyncCleanup(self.web.aclose)

    async def states(self):
        async with self.db.sessions() as session:
            return [
                (d.provisioning_id, d.status)
                for d in await session.scalars(
                    select(Device)
                    .where(Device.subscription_id == self.sid)
                    .order_by(Device.id)
                )
            ]

    async def test_original_missing_devices_preserves_local_records(self):
        response = await self.detail()
        self.assertEqual(response["device_sync"]["status"], "unavailable")
        self.assertEqual(await self.states(), [("a", "active"), ("b", "active")])

    async def detail(self):
        response = await self.web.get(f"/api/web/keys/{self.kid}/devices")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def sync(self, api=None):
        async with self.db.sessions.begin() as session:
            credential = await session.get(VlessCredential, self.cid)
            return await synchronize(
                SQLRepository(session), credential, api or self.api
            )

    async def test_valid_empty_is_authoritative(self):
        self.api.devices.return_value = snapshot()
        response = await self.detail()
        self.assertEqual(response["device_sync"]["status"], "confirmed")
        self.assertEqual(response["device_sync"]["count"], 0)
        self.assertEqual(
            await self.states(), [("a", "disconnected"), ("b", "disconnected")]
        )

    async def test_valid_list_updates_only_confirmed_devices(self):
        self.api.devices.return_value = snapshot(hwid("a"), hwid("c"))
        await self.sync()
        self.assertEqual(
            await self.states(),
            [("a", "active"), ("b", "disconnected"), ("c", "active")],
        )

    async def test_failure_after_valid_sync_retains_timestamp_and_count(self):
        self.api.devices.return_value = snapshot(hwid("a"), hwid("b"))
        good = await self.detail()
        self.api.devices.side_effect = RemnawaveApiError("offline", 503)
        stale = await self.detail()
        self.assertEqual(stale["device_sync"]["status"], "unavailable")
        self.assertEqual(
            stale["device_sync"]["last_successful_at"],
            good["device_sync"]["last_successful_at"],
        )
        self.assertEqual(stale["device_sync"]["count"], 2)
        self.assertEqual(await self.states(), [("a", "active"), ("b", "active")])

    async def test_recovery_from_malformed_snapshot(self):
        await self.detail()
        self.api.devices.return_value = snapshot(hwid("b"))
        response = await self.detail()
        self.assertEqual(response["device_sync"]["status"], "confirmed")
        self.assertEqual(await self.states(), [("a", "disconnected"), ("b", "active")])

    async def test_never_observed_is_unknown_during_failure(self):
        async with self.db.sessions.begin() as session:
            await session.execute(
                delete(Device).where(Device.subscription_id == self.sid)
            )
        response = await self.detail()
        self.assertIsNone(response["device_sync"]["count"])

    async def test_restart_recovers_persistent_cache(self):
        self.api.devices.return_value = snapshot(hwid("b"))
        await self.sync()
        await self.db.engine.dispose()
        self.api.devices.side_effect = TimeoutError()
        response = await self.detail()
        self.assertEqual(response["device_sync"]["count"], 1)
        self.assertIsNotNone(response["device_sync"]["last_successful_at"])

    async def test_rollback_preserves_cache_and_allows_retry(self):
        self.api.devices.return_value = snapshot()
        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            async with self.db.sessions.begin() as session:
                credential = await session.get(VlessCredential, self.cid)
                await synchronize(SQLRepository(session), credential, self.api)
                raise RuntimeError("commit failed")
        self.assertEqual(await self.states(), [("a", "active"), ("b", "active")])
        async with self.db.sessions() as session:
            self.assertEqual(await cache_state(session, self.sid), (2, None))
        await self.sync()
        self.assertEqual(
            await self.states(), [("a", "disconnected"), ("b", "disconnected")]
        )

    async def ordered_syncs(self, first):
        arrived = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]

        async def read(index):
            arrived[index].set()
            await release[index].wait()
            return (
                snapshot() if index == 0 else snapshot(hwid("a"), hwid("b"), hwid("c"))
            )

        tasks = [
            asyncio.create_task(
                self.sync(SimpleNamespace(devices=lambda _, i=i: read(i)))
            )
            for i in range(2)
        ]
        try:
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in arrived)), 5
            )
            # A separate transaction can acquire the row while HTTP is blocked.
            async with self.db.sessions.begin() as session:
                await asyncio.wait_for(
                    session.scalar(
                        select(Subscription.id)
                        .where(Subscription.id == self.sid)
                        .with_for_update()
                    ),
                    1,
                )
            release[first].set()
            self.assertIsNotNone(await asyncio.wait_for(tasks[first], 5))
            release[1 - first].set()
            self.assertIsNone(await asyncio.wait_for(tasks[1 - first], 5))
            expected = (
                [("a", "disconnected"), ("b", "disconnected")]
                if first == 0
                else [("a", "active"), ("b", "active"), ("c", "active")]
            )
            self.assertEqual(await self.states(), expected)
        finally:
            for event in release:
                event.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_concurrent_empty_finishes_first(self):
        await self.ordered_syncs(0)

    async def test_concurrent_nonempty_finishes_first(self):
        await self.ordered_syncs(1)

    async def test_concurrent_initial_sync_creates_each_hwid_once(self):
        async with self.db.sessions.begin() as session:
            await session.execute(
                delete(Device).where(Device.subscription_id == self.sid)
            )
        self.api.devices.return_value = snapshot(hwid("a"), hwid("b"))
        await asyncio.gather(*(self.sync() for _ in range(5)))
        self.assertEqual(await self.states(), [("a", "active"), ("b", "active")])

    async def test_explicit_removal_survives_inflight_snapshot(self):
        arrived, release = asyncio.Event(), asyncio.Event()

        async def read(_):
            arrived.set()
            await release.wait()
            return snapshot(hwid("a"), hwid("b"))

        task = asyncio.create_task(self.sync(SimpleNamespace(devices=read)))
        try:
            await asyncio.wait_for(arrived.wait(), 5)
            async with self.db.sessions() as session:
                device = await session.scalar(
                    select(Device).where(Device.provisioning_id == "a")
                )
            response = await self.web.delete(
                f"/api/web/keys/{self.kid}/devices/{device.id}"
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.api.delete_device.assert_awaited_once_with("a", "123")
            release.set()
            self.assertIsNone(await asyncio.wait_for(task, 5))
            self.assertEqual(
                await self.states(), [("a", "disconnected"), ("b", "active")]
            )
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_failed_explicit_removal_preserves_cache(self):
        self.api.delete_device.side_effect = RemnawaveApiError("offline", 503)
        async with self.db.sessions() as session:
            device = await session.scalar(
                select(Device).where(Device.provisioning_id == "a")
            )
        response = await self.web.delete(
            f"/api/web/keys/{self.kid}/devices/{device.id}"
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(await self.states(), [("a", "active"), ("b", "active")])

    async def test_explicit_reset_without_valid_list(self):
        response = await self.web.post(
            f"/api/web/keys/{self.kid}/devices/reset",
            json=dict(
                action="reset_devices", confirmed=True, request_id="i06-reset-fixture"
            ),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.api.delete_all_devices.assert_awaited_once_with("123")
        self.api.devices.assert_not_awaited()
        self.assertEqual(await self.states(), [("a", "removed"), ("b", "removed")])

    async def test_no_paid_terms_changed_by_sync(self):
        self.api.devices.return_value = snapshot()
        await self.sync()
        async with self.db.sessions() as session:
            sub = await session.get(Subscription, self.sid)
            credential = await session.get(VlessCredential, self.cid)
            self.assertEqual(sub.expires_at, self.expiry)
            self.assertEqual(credential.expires_at, self.expiry)
            self.assertEqual(credential.device_limit, 1)

    async def test_admin_details_and_list_keep_saved_count_on_failure(self):
        self.settings.set_staff_role(self.user.telegram_id, "administrator")
        response = await self.web.get(f"/api/web/admin/keys/{self.kid}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["key"]["device_count"], 2)
        response = await self.web.get("/api/web/admin/keys")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["device_count"], 2)

    async def test_confirmed_zero_survives_unavailable_read(self):
        self.api.devices.return_value = snapshot()
        await self.sync()
        self.api.devices.side_effect = TimeoutError()
        response = await self.detail()
        self.assertEqual(response["device_sync"]["count"], 0)
        self.assertIsNotNone(response["device_sync"]["last_successful_at"])

    async def test_replaced_remote_identity_discards_inflight_snapshot(self):
        async def read(_):
            async with self.db.sessions.begin() as session:
                credential = await session.get(VlessCredential, self.cid)
                credential.remote_user_uuid = "456"
            return snapshot()

        self.assertIsNone(await self.sync(SimpleNamespace(devices=read)))
        self.assertEqual(await self.states(), [("a", "active"), ("b", "active")])

    async def test_reset_of_empty_cache_fences_inflight_nonempty_snapshot(self):
        async with self.db.sessions.begin() as session:
            await session.execute(
                delete(Device).where(Device.subscription_id == self.sid)
            )

        async def read(_):
            async with self.db.sessions.begin() as session:
                credential = await session.get(VlessCredential, self.cid)
                await confirmed_removal(SQLRepository(session), credential)
            return snapshot(hwid())

        self.assertIsNone(await self.sync(SimpleNamespace(devices=read)))
        self.assertEqual(await self.states(), [])

    async def test_telegram_rejects_malformed_list(self):
        from app.services.vless_devices import render

        async with self.db.sessions.begin() as session:
            with self.assertRaises(ValueError):
                await render(
                    SQLRepository(session),
                    self.settings,
                    self.user,
                    f"vd:list:{self.sid}:0",
                )
        self.assertEqual(await self.states(), [("a", "active"), ("b", "active")])


def invalid_case(payload):
    async def test(self):
        self.api.devices.return_value = copy.deepcopy(payload)
        response = await self.detail()
        self.assertEqual(response["device_sync"]["status"], "unavailable")
        self.assertEqual(response["device_sync"]["count"], 2)
        self.assertEqual(await self.states(), [("a", "active"), ("b", "active")])

    return test


for name, payload in MALFORMED.items():
    setattr(VlessDevices, "test_reject_" + name, invalid_case(payload))


def failure_case(error):
    async def test(self):
        self.api.devices.side_effect = error
        response = await self.detail()
        self.assertEqual(response["device_sync"]["status"], "unavailable")
        self.assertEqual(response["device_sync"]["count"], 2)
        self.assertEqual(await self.states(), [("a", "active"), ("b", "active")])

    return test


for name, error in {
    "timeout": TimeoutError(),
    "missing_user": RemnawaveApiError("missing", 404),
    "api_error": RemnawaveApiError("offline", 503),
}.items():
    setattr(VlessDevices, "test_preserve_on_" + name, failure_case(error))


class HwidContract(unittest.TestCase):
    def test_complete_contract(self):
        self.assertEqual(parse_devices(snapshot(hwid()), "123"), [hwid()])

    def test_valid_empty(self):
        self.assertEqual(parse_devices(snapshot(), "123"), [])

    def test_all_incomplete_contracts(self):
        for name, payload in MALFORMED.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                parse_devices(payload, "123")
