"""I07 PostgreSQL panel counters and installed 3.4.4 statistics contracts."""

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import event, select
from sqlalchemy.orm import Session
from test_billing_concurrency import BillingConcurrency

from app.database.models import AppSetting, RemnawaveServer, VlessCredential, now
from app.database.repositories import SQLRepository
from app.services.remnawave import (
    RemnawaveApiClient,
    RemnawaveApiError,
    RemnawaveService,
)
from app.services.remnawave_stats import key, observation


def stats(traffic="0", total=0, online=0, timestamp=None):
    return {
        "response": {
            "cpu": {"cores": 2},
            "memory": {"total": 100, "free": 50, "used": 50},
            "uptime": 100,
            "timestamp": timestamp
            if timestamp is not None
            else int(now().timestamp() * 1000),
            "users": {"statusCounts": {}, "totalUsers": total},
            "onlineStats": {
                "lastDay": 0,
                "lastWeek": 0,
                "neverOnline": 0,
                "onlineNow": online,
            },
            "nodes": {"totalOnline": online, "totalBytesLifetime": traffic},
        }
    }


def users(total=0):
    return {"response": {"total": total, "users": [{"id": 123}] if total else []}}


def broken():
    cases = []
    for path in (
        ("nodes", "totalBytesLifetime"),
        ("onlineStats", "onlineNow"),
        ("users", "totalUsers"),
        ("timestamp",),
    ):
        for value in (None, "missing"):
            payload = stats()
            target = payload["response"]
            for field in path[:-1]:
                target = target[field]
            if value == "missing":
                target.pop(path[-1])
            else:
                target[path[-1]] = value
            cases.append((str(path) + str(value), payload, users()))
    for value in (0, True, "-1", "1.5", "nan", "", " 0", str(2**63), "9" * 100):
        cases.append(("traffic:" + str(value), stats(traffic=value), users()))
    for value in (True, -1, 1.5, "0", 2**31):
        cases.append(("online:" + str(value), stats(online=value), users()))
        cases.append(
            (
                "total:" + str(value),
                stats(),
                {"response": {"total": value, "users": []}},
            )
        )
    cases.extend(
        [
            ("missing envelope", {}, users()),
            ("null parent", {"response": {"nodes": None}}, users()),
            ("absent users", stats(), {"response": {"total": 0}}),
            (
                "partial user page",
                stats(total=5),
                {"response": {"total": 5, "users": []}},
            ),
            (
                "malformed user page",
                stats(total=1),
                {"response": {"total": 1, "users": [None]}},
            ),
            (
                "old timestamp",
                stats(timestamp=int(now().timestamp() * 1000) - 600_000),
                users(),
            ),
            (
                "future timestamp",
                stats(timestamp=int(now().timestamp() * 1000) + 600_000),
                users(),
            ),
            ("boolean timestamp", stats(timestamp=True), users()),
            ("legacy flat data", {"trafficUsed": 0, "onlineUsers": 0}, users()),
        ]
    )
    return cases


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class PanelStatistics(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await BillingConcurrency.asyncSetUp(self)
        self.sid, self.kid, self.expiry = await BillingConcurrency.subscription(self)
        async with self.db.sessions.begin() as session:
            panel = await session.scalar(select(RemnawaveServer))
            self.pid = panel.id
            panel.users_total, panel.users_online, panel.traffic_used = 7, 3, 99
        self.api = AsyncMock()
        self.api.health.return_value = {}
        self.api.stats.return_value = stats()
        self.api.users.return_value = {"response": {"total": 0, "users": []}}
        item = patch(
            "app.services.remnawave.RemnawaveService.api", return_value=self.api
        )
        item.start()
        self.addCleanup(item.stop)

    async def refresh(self):
        async with self.db.sessions.begin() as session:
            panel = await session.get(RemnawaveServer, self.pid)
            return await RemnawaveService(
                SQLRepository(session), self.settings
            ).refresh(panel)

    async def state(self):
        async with self.db.sessions() as session:
            return await session.get(RemnawaveServer, self.pid)

    async def metadata(self):
        async with self.db.sessions() as session:
            raw = await session.get(AppSetting, key(self.pid))
            return json.loads(raw.value) if raw else {}

    async def valid(self, traffic="123", total=2, online=1):
        previous = await self.metadata()
        stamp = max(
            int(now().timestamp() * 1000), previous.get("remote_timestamp", 0) + 1
        )
        self.api.stats.return_value = stats(traffic, total, online, stamp)
        self.api.users.return_value = users(total)

    async def test_original_zero_traffic_updates_cached_statistics(self):
        self.assertTrue(await self.refresh())
        panel = await self.state()
        self.assertEqual(
            (panel.users_total, panel.users_online, panel.traffic_used), (0, 0, 0)
        )

    async def test_large_decimal_traffic_is_exact(self):
        value = 2**60 + 17
        await self.valid(str(value))
        self.assertTrue(await self.refresh())
        panel = await self.state()
        self.assertEqual(
            (panel.users_total, panel.users_online, panel.traffic_used), (2, 1, value)
        )

    async def test_newer_confirmed_zero_replaces_positive_aggregate(self):
        await self.valid()
        await self.refresh()
        await self.valid("0", 0, 0)
        self.assertTrue(await self.refresh())
        self.assertEqual((await self.state()).traffic_used, 0)

    async def test_invalid_data_preserves_all_counters(self):
        for name, payload, listing in broken():
            with self.subTest(name=name):
                self.api.stats.return_value, self.api.users.return_value = (
                    payload,
                    listing,
                )
                self.assertFalse(await self.refresh())
                panel = await self.state()
                self.assertEqual(
                    (panel.users_total, panel.users_online, panel.traffic_used),
                    (7, 3, 99),
                )
                self.assertIsNone(panel.last_sync)

    async def test_missing_data_preserves_last_success_timestamp(self):
        await self.valid()
        await self.refresh()
        before = await self.state()
        self.api.stats.return_value = {"response": {}}
        self.assertFalse(await self.refresh())
        after = await self.state()
        self.assertEqual(
            (after.traffic_used, after.users_online, after.last_sync),
            (before.traffic_used, before.users_online, before.last_sync),
        )
        metadata = await self.metadata()
        self.assertEqual(metadata["outcome"], "unavailable")
        self.assertEqual(metadata["last_success_at"], before.last_sync.isoformat())
        self.assertIn("last_attempt_at", metadata)

    async def test_api_failures_and_timeouts_do_not_zero_counters(self):
        for failure in (
            TimeoutError(),
            RemnawaveApiError("503", 503),
            RemnawaveApiError("429", 429),
            RemnawaveApiError("401", 401),
        ):
            with self.subTest(error=str(failure)):
                self.api.stats.side_effect = failure
                self.assertFalse(await self.refresh())
                panel = await self.state()
                self.assertEqual(
                    (panel.users_total, panel.users_online, panel.traffic_used),
                    (7, 3, 99),
                )

    async def test_retry_recovers_zero_after_failure(self):
        self.api.stats.side_effect = RemnawaveApiError("offline", 503)
        self.assertFalse(await self.refresh())
        self.api.stats.side_effect = None
        await self.valid("0", 0, 0)
        self.assertTrue(await self.refresh())
        self.assertEqual((await self.state()).traffic_used, 0)
        self.assertEqual((await self.state()).health, "online")

    async def test_restart_preserves_ordering_and_cache(self):
        await self.valid()
        await self.refresh()
        stamp = (await self.metadata())["remote_timestamp"]
        await self.db.engine.dispose()
        self.api.stats.return_value = stats(timestamp=stamp - 1)
        self.assertFalse(await self.refresh())
        self.assertEqual((await self.state()).traffic_used, 123)
        self.assertEqual((await self.metadata())["outcome"], "stale")

    async def ordered(self, first, failed_old=False):
        ready, release = (
            [asyncio.Event(), asyncio.Event()],
            [asyncio.Event(), asyncio.Event()],
        )
        stamp = int(now().timestamp() * 1000)
        replies = [stats("0", 0, 0, stamp - 1000), stats("900", 5, 2, stamp)]

        async def read(index):
            ready[index].set()
            await release[index].wait()
            if failed_old and index == 0:
                raise RemnawaveApiError("old failure", 503)
            return replies[index]

        async def poll(index):
            api = SimpleNamespace(
                health=AsyncMock(return_value={}),
                stats=lambda: read(index),
                users=AsyncMock(return_value=users(0 if index == 0 else 5)),
            )
            async with self.db.sessions.begin() as session:
                service = RemnawaveService(SQLRepository(session), self.settings)
                service.api = lambda _: api
                return await service.refresh(
                    await session.get(RemnawaveServer, self.pid)
                )

        tasks = [asyncio.create_task(poll(i)) for i in range(2)]
        try:
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in ready)), 5)
            async with self.db.sessions.begin() as session:
                await asyncio.wait_for(
                    session.scalar(
                        select(RemnawaveServer.id)
                        .where(RemnawaveServer.id == self.pid)
                        .with_for_update()
                    ),
                    1,
                )
            release[first].set()
            self.assertTrue(await asyncio.wait_for(tasks[first], 5))
            release[1 - first].set()
            result = await asyncio.wait_for(tasks[1 - first], 5)
            self.assertEqual(result, first == 0 and not failed_old)
            panel = await self.state()
            self.assertEqual(
                (
                    panel.traffic_used,
                    panel.users_total,
                    panel.users_online,
                    panel.health,
                ),
                (900, 5, 2, "online"),
            )
        finally:
            for e in release:
                e.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_newer_response_finishes_first(self):
        await self.ordered(1)

    async def test_newer_response_finishes_last(self):
        await self.ordered(0)

    async def test_old_failure_cannot_mark_newer_success_offline(self):
        await self.ordered(1, failed_old=True)

    async def test_equal_timestamp_cannot_replace_observation(self):
        await self.valid()
        await self.refresh()
        stamp = (await self.metadata())["remote_timestamp"]
        self.api.stats.return_value = stats(timestamp=stamp)
        self.api.users.return_value = users()
        self.assertFalse(await self.refresh())
        self.assertEqual((await self.state()).traffic_used, 123)

    async def test_allocation_during_get_keeps_capacity_guard(self):
        async def listing(*_):
            async with self.db.sessions.begin() as session:
                panel = await session.get(
                    RemnawaveServer, self.pid, with_for_update=True
                )
                panel.users_total, panel.capacity_revision = (
                    8,
                    panel.capacity_revision + 1,
                )
            return users()

        self.api.users.side_effect = listing
        self.assertTrue(await self.refresh())
        panel = await self.state()
        self.assertEqual((panel.users_total, panel.traffic_used), (8, 0))
        self.assertTrue((await self.metadata())["capacity_deferred"])

    async def test_reconfigured_source_discards_inflight_data(self):
        async def listing(*_):
            async with self.db.sessions.begin() as session:
                (
                    await session.get(RemnawaveServer, self.pid)
                ).api_url = "https://other.invalid"
            return users()

        self.api.users.side_effect = listing
        self.assertFalse(await self.refresh())
        self.assertEqual((await self.state()).traffic_used, 99)

    async def test_caller_late_commit_cannot_restore_older_stats(self):
        async with self.db.sessions.begin() as session:
            service = RemnawaveService(SQLRepository(session), self.settings)
            panel = await session.get(RemnawaveServer, self.pid)
            self.assertTrue(await service.refresh(panel))
            await self.valid("555", 5, 2)
            self.assertTrue(await self.refresh())
        self.assertEqual((await self.state()).traffic_used, 555)

    async def test_failed_statistics_commit_rolls_back_and_retry_recovers(self):
        original = SQLRepository.set_setting

        async def mark(repo, name, value):
            await original(repo, name, value)
            if name == key(self.pid):
                repo.session.sync_session.info["i07_fail_commit"] = True

        def reject(session):
            if session.info.get("i07_fail_commit"):
                raise RuntimeError("I07 commit failure")

        event.listen(Session, "before_commit", reject)
        try:
            with (
                patch.object(SQLRepository, "set_setting", new=mark),
                self.assertRaisesRegex(RuntimeError, "I07 commit failure"),
            ):
                await self.refresh()
        finally:
            event.remove(Session, "before_commit", reject)
        panel = await self.state()
        self.assertEqual(
            (panel.users_total, panel.users_online, panel.traffic_used), (7, 3, 99)
        )
        self.assertEqual(await self.metadata(), {})
        self.assertTrue(await self.refresh())

    async def test_paid_key_limits_usage_and_reset_policy_are_unchanged(self):
        async with self.db.sessions.begin() as session:
            credential = await session.scalar(select(VlessCredential))
            credential.traffic_limit, credential.traffic_used = 1000, 73
            cid = credential.id
        self.assertTrue(await self.refresh())
        async with self.db.sessions() as session:
            credential = await session.get(VlessCredential, cid)
            self.assertEqual(
                (
                    credential.traffic_limit,
                    credential.traffic_used,
                    credential.expires_at,
                ),
                (1000, 73, self.expiry),
            )
        self.api.reset_traffic.assert_not_awaited()
        self.api.update_user.assert_not_awaited()


class StatisticsContract(unittest.IsolatedAsyncioTestCase):
    def test_official_zero_and_bigint_contract(self):
        self.assertEqual(observation(stats(), users())["traffic_used"], 0)
        self.assertEqual(
            observation(stats(str(2**63 - 1)), users())["traffic_used"], 2**63 - 1
        )

    def test_all_invalid_shapes_are_rejected(self):
        for name, payload, listing in broken():
            with self.subTest(name=name), self.assertRaises(ValueError):
                observation(payload, listing)

    async def test_real_client_paths_and_paginated_count_contract(self):
        replies = [stats(), users()]
        calls = []

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def json(self, **_):
                return replies.pop(0)

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            def request(self, method, url, **kwargs):
                calls.append((method, url, kwargs.get("params")))
                return Response()

        with patch(
            "app.services.remnawave.aiohttp.ClientSession", return_value=Client()
        ):
            api = RemnawaveApiClient("https://panel.example.test", "fixture")
            self.assertEqual(
                observation(await api.stats(), await api.users(0, 1))["traffic_used"], 0
            )
        self.assertEqual(
            calls,
            [
                ("GET", "https://panel.example.test/api/system/stats", None),
                (
                    "GET",
                    "https://panel.example.test/api/users",
                    {"start": 0, "size": 1},
                ),
            ],
        )
