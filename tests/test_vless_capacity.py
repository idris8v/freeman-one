"""I03 PostgreSQL admission/recovery; all remote and payment APIs are fixtures."""

import asyncio
import json
import os
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import event, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_billing_concurrency import BillingConcurrency
from test_vless_creation import Panel

from app.database.models import (
    RemnawaveServer,
    Subscription,
    User,
    VlessCreation,
    VlessCredential,
    VlessReservation,
    now,
)
from app.database.repositories import SQLRepository
from app.database.vless_capacity_migration import upgrade
from app.services import vless_capacity as capacity
from app.services import vless_creation, vless_provision
from app.services.remnawave import RemnawaveService


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class VlessCapacity(unittest.IsolatedAsyncioTestCase):
    checkout = BillingConcurrency.checkout

    async def asyncSetUp(self):
        await BillingConcurrency.asyncSetUp(self)
        self.api = Panel()
        item = patch(
            "app.services.remnawave.RemnawaveService.api",
            side_effect=lambda *_: self.api,
        )
        item.start()
        self.addCleanup(item.stop)
        async with self.db.sessions.begin() as session:
            panel = RemnawaveServer(
                name="Last slot",
                country="TEST",
                api_url="https://panel.example.test",
                api_token="fixture",
                internal_squad_uuid="fixture-squad",
                health="online",
                max_users=1,
            )
            session.add(panel)
            await session.flush()
            self.pid = panel.id
            self.sids = []
            for uid in (self.uid, self.other):
                sub = Subscription(user_id=uid, expires_at=now() + timedelta(days=30))
                session.add(sub)
                await session.flush()
                self.sids.append(sub.id)

    async def issue(self, index, *, explicit=False, rollback=False):
        async with self.db.sessions.begin() as session:
            sub = await session.get(Subscription, self.sids[index])
            user = await session.get(User, sub.user_id)
            repo = SQLRepository(session)
            if explicit:
                credential = await vless_provision.provision(
                    repo,
                    self.settings,
                    sub,
                    user,
                    await session.get(RemnawaveServer, self.pid),
                    self.user,
                )
            else:
                credential = await vless_provision.ensure(
                    repo, self.settings, sub, user
                )
            if rollback:
                raise RuntimeError("Injected local rollback")
            return credential.id

    async def snapshot(self):
        async with self.db.sessions() as session:
            panel = await session.get(RemnawaveServer, self.pid)
            rows = list(await session.scalars(select(VlessReservation)))
            keys = await session.scalar(
                select(func.count()).select_from(VlessCredential)
            )
            return panel.users_total, sorted(r.state for r in rows), keys

    async def configure(self, **values):
        async with self.db.sessions.begin() as session:
            panel = await session.get(RemnawaveServer, self.pid)
            for key, value in values.items():
                setattr(panel, key, value)

    async def compete(self, winner=0, **options):
        self.api.entered, self.api.release = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(self.issue(winner, **options))
        try:
            await asyncio.wait_for(self.api.entered.wait(), 10)
            self.assertEqual((await self.snapshot())[:2], (0, ["reserved"]))
            # Admission cannot wait on locks held over the first remote POST.
            with self.assertRaises(ValueError):
                await asyncio.wait_for(self.issue(1 - winner), 3)
            async with self.db.sessions.begin() as session:
                for model in (RemnawaveServer, VlessCreation, VlessReservation):
                    self.assertIsNotNone(
                        await session.scalar(select(model).with_for_update(nowait=True))
                    )
        finally:
            self.api.release.set()
        await asyncio.wait_for(first, 10)
        self.assertEqual(len(self.api.creates), 1)
        self.assertEqual(len(self.api.remote), 1)
        self.assertEqual(await self.snapshot(), (1, ["consumed"], 1))

    async def test_two_clients_competing_for_final_slot(self):
        await self.compete()

    async def test_final_slot_reversed_request_order(self):
        await self.compete(winner=1)

    async def test_explicit_admin_and_automatic_creation_share_capacity(self):
        await self.compete(explicit=True)

    async def test_existing_remote_users_are_included(self):
        await self.configure(max_users=5, users_total=4)
        await self.issue(0)
        with self.assertRaises(ValueError):
            await self.issue(1, explicit=True)
        self.assertEqual(await self.snapshot(), (5, ["consumed"], 1))
        self.assertEqual(len(self.api.creates), 1)

    async def test_same_operation_concurrent_requests_consume_once(self):
        self.api.entered, self.api.release = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(self.issue(0))
        second = None
        try:
            await asyncio.wait_for(self.api.entered.wait(), 10)
            second = asyncio.create_task(self.issue(0))
            await asyncio.sleep(0.2)
            self.assertFalse(second.done())
        finally:
            self.api.release.set()
        ids = await asyncio.wait_for(asyncio.gather(first, second), 15)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(len(self.api.creates), 1)
        self.assertEqual(await self.snapshot(), (1, ["consumed"], 1))

    async def test_different_slots_do_not_serialize_remote_http(self):
        await self.configure(max_users=2)
        self.api.entered, self.api.release = asyncio.Event(), asyncio.Event()
        tasks = [asyncio.create_task(self.issue(i)) for i in range(2)]
        try:

            async def both_remote():
                while len(self.api.creates) != 2:
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(both_remote(), 5)
            self.assertEqual(await self.snapshot(), (0, ["reserved", "reserved"], 0))
        finally:
            self.api.release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        self.assertEqual(await self.snapshot(), (2, ["consumed", "consumed"], 2))

    async def test_reserved_full_panel_falls_back_before_any_remote_call(self):
        self.api.creation_error = 503
        with self.assertRaises(ValueError):
            await self.issue(0)
        async with self.db.sessions.begin() as session:
            session.add(
                RemnawaveServer(
                    name="Other",
                    country="TEST",
                    api_url="https://other.example.test",
                    api_token="fixture",
                    internal_squad_uuid="other",
                    health="online",
                    max_users=1,
                    priority=100,
                )
            )
        self.api.creation_error = None
        await self.issue(1)
        async with self.db.sessions() as session:
            credential = await session.scalar(select(VlessCredential))
            self.assertNotEqual(credential.server_id, self.pid)
        self.assertEqual((await self.snapshot())[:2], (0, ["consumed", "reserved"]))
        self.assertEqual(len(self.api.creates), 2)  # One failed POST, one successful.

    async def test_confirmed_rejection_releases_but_retry_must_reserve_again(self):
        self.api.creation_error = 401
        with self.assertRaises(ValueError):
            await self.issue(0)
        self.assertEqual(await self.snapshot(), (0, ["released"], 0))
        self.api.creation_error = None
        await self.issue(1)
        with self.assertRaises(ValueError):
            await self.issue(0)
        self.assertEqual(len(self.api.creates), 2)
        self.assertEqual(await self.snapshot(), (1, ["consumed", "released"], 1))

    async def test_unknown_failure_does_not_release_on_later_rejection(self):
        self.api.creation_error = 503
        with self.assertRaises(ValueError):
            await self.issue(0)
        self.api.creation_error = 401
        with self.assertRaises(ValueError):
            await self.issue(0)
        self.assertEqual(await self.snapshot(), (0, ["reserved"], 0))
        with self.assertRaises(ValueError):
            await self.issue(1)
        self.assertEqual(len(self.api.creates), 2)
        self.api.creation_error = None
        await self.issue(0)
        self.assertEqual(await self.snapshot(), (1, ["consumed"], 1))

    async def test_remote_timeout_retains_slot_and_restart_recovers_once(self):
        self.api.lose_response = True
        with self.assertRaises(ValueError):
            await self.issue(0)
        self.assertEqual(await self.snapshot(), (0, ["reserved"], 0))
        with self.assertRaises(ValueError):
            await self.issue(1)
        self.api = Panel(self.api.remote)
        await self.db.engine.dispose()
        await self.issue(0)
        self.assertEqual(self.api.creates, [])
        self.assertEqual(await self.snapshot(), (1, ["consumed"], 1))

    async def test_local_rollback_keeps_confirmed_remote_occupancy(self):
        with self.assertRaises(RuntimeError):
            await self.issue(0, rollback=True)
        self.assertEqual(await self.snapshot(), (1, ["consumed"], 0))
        with self.assertRaises(ValueError):
            await self.issue(1)
        await self.issue(0)
        self.assertEqual(len(self.api.creates), 1)
        self.assertEqual(await self.snapshot(), (1, ["consumed"], 1))

    async def test_confirmation_commit_failure_keeps_pending_reservation(self):
        armed = True

        def fail(session):
            nonlocal armed
            if armed and any(
                isinstance(r, VlessCreation) and r.state == "remote_confirmed"
                for r in session.dirty
            ):
                armed = False
                raise SQLAlchemyError("Injected confirmation commit failure")

        event.listen(Session, "before_commit", fail)
        try:
            with self.assertRaises(ValueError):
                await self.issue(0)
        finally:
            event.remove(Session, "before_commit", fail)
        self.assertFalse(armed)
        self.assertEqual(await self.snapshot(), (0, ["reserved"], 0))
        with self.assertRaises(ValueError):
            await self.issue(1)
        await self.issue(0)
        self.assertEqual(len(self.api.creates), 1)
        self.assertEqual(await self.snapshot(), (1, ["consumed"], 1))

    async def test_worker_cancellation_expires_lease_but_never_capacity(self):
        self.api.entered, self.api.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.issue(0))
        await asyncio.wait_for(self.api.entered.wait(), 10)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        async with self.db.sessions.begin() as session:
            reservation = await session.scalar(select(VlessReservation))
            self.assertTrue(reservation.lease_token)
            reservation.lease_until = now() - timedelta(seconds=1)
        with self.assertRaises(ValueError):
            await self.issue(1)
        self.api = Panel(self.api.remote)
        await self.db.engine.dispose()
        await self.issue(0)
        self.assertEqual(self.api.creates, [])
        self.assertEqual(await self.snapshot(), (1, ["consumed"], 1))

    async def test_stale_worker_cannot_release_new_lease(self):
        async with self.db.sessions() as session:
            repo = SQLRepository(session)
            customer = f"user:{self.uid}"
            identity = vless_creation.creation_id(
                customer, f"subscription:{self.sids[0]}"
            )
            body = {"expireAt": now().isoformat(), "fixture": "same"}
            args = (
                repo,
                identity,
                customer,
                self.uid,
                self.pid,
                "https://panel.example.test",
                body,
            )
            old = await capacity.claim(*args)
            async with self.db.sessions.begin() as other:
                row = await other.get(VlessReservation, identity)
                row.lease_until = now() - timedelta(seconds=1)
            new = await capacity.claim(*args)
            self.assertNotEqual(old.token, new.token)
            with self.assertRaises(ValueError):
                await capacity.finish(
                    repo, identity, old, rejected=True, failure=ValueError()
                )
            self.assertEqual(await self.snapshot(), (0, ["reserved"], 0))
            await capacity.finish(repo, identity, new, result={"identity": "100"})
            self.assertEqual(await self.snapshot(), (1, ["consumed"], 0))

    async def test_admission_commit_failure_makes_no_remote_call(self):
        armed = True

        def fail(session):
            nonlocal armed
            if armed and any(isinstance(r, VlessReservation) for r in session.new):
                armed = False
                raise SQLAlchemyError("Injected reservation commit failure")

        event.listen(Session, "before_commit", fail)
        try:
            with self.assertRaises(ValueError):
                await self.issue(0)
        finally:
            event.remove(Session, "before_commit", fail)
        self.assertFalse(armed)
        self.assertEqual(self.api.lookups, [])
        self.assertEqual(self.api.creates, [])
        self.assertEqual(await self.snapshot(), (0, [], 0))
        await self.issue(1)

    async def test_local_association_lock_does_not_block_other_panel_admissions(self):
        await self.configure(max_users=2)
        with self.assertRaises(RuntimeError):
            await self.issue(0, rollback=True)
        async with self.db.sessions() as session:
            repo = SQLRepository(session)
            operation = await session.scalar(select(VlessCreation))
            args = (
                repo,
                operation.id,
                operation.customer_ref,
                self.uid,
                self.pid,
                operation.server_url,
                json.loads(operation.request),
            )
            claim = await capacity.claim(*args)
            result = json.loads(operation.result)
            async with self.db.sessions.begin() as blocker:
                await blocker.get(VlessCreation, operation.id, with_for_update=True)
                self.assertIsNone(await asyncio.wait_for(capacity.claim(*args), 3))
                finishing = asyncio.create_task(
                    capacity.finish(
                        repo,
                        operation.id,
                        claim,
                        result=result,
                    )
                )
                try:
                    await asyncio.sleep(0.2)
                    self.assertFalse(finishing.done())
                    await asyncio.wait_for(self.issue(1), 3)
                finally:
                    # Release the simulated association even on assertion failure.
                    await blocker.commit()
                    await asyncio.wait_for(finishing, 5)
        self.assertEqual(await self.snapshot(), (2, ["consumed", "consumed"], 1))

    async def test_stale_refresh_cannot_erase_newly_occupied_slot(self):
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            panel = await session.get(RemnawaveServer, self.pid)
            version = await capacity.refresh_version(repo, self.pid)
            await self.issue(0)
            await capacity.refresh_total(repo, panel, version, 0)
            self.assertEqual(panel.users_total, 1)
        with self.assertRaises(ValueError):
            await self.issue(1)
        self.assertEqual(await self.snapshot(), (1, ["consumed"], 1))

    async def test_pending_refresh_does_not_double_count_uncertain_remote(self):
        self.api.lose_response = True
        with self.assertRaises(ValueError):
            await self.issue(0)
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            panel = await session.get(RemnawaveServer, self.pid)
            version = await capacity.refresh_version(repo, self.pid)
            await capacity.refresh_total(repo, panel, version, 1)
            self.assertEqual(panel.users_total, 0)
        self.api.lose_response = self.api.lookup_error = False
        await self.issue(0)
        self.assertEqual(await self.snapshot(), (1, ["consumed"], 1))

    async def test_refresh_orm_copy_does_not_overwrite_later_confirmation(self):
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            panel = await session.get(RemnawaveServer, self.pid)
            version = await capacity.refresh_version(repo, self.pid)
            await capacity.refresh_total(repo, panel, version, 0)
            await self.issue(0)
            panel.health = "online"
        self.assertEqual(await self.snapshot(), (1, ["consumed"], 1))

    async def test_service_refresh_accepts_real_zero_and_respects_reservations(self):
        self.api.health = AsyncMock(return_value={})
        self.api.stats = AsyncMock(
            side_effect=lambda: {
                "response": {
                    "timestamp": int(now().timestamp() * 1000),
                    "users": {"totalUsers": 99},
                    "onlineStats": {"onlineNow": 0},
                    "nodes": {"totalBytesLifetime": "0"},
                }
            }
        )
        self.api.users = AsyncMock(return_value={"response": {"total": 0, "users": []}})
        await self.configure(users_total=1)
        async with self.db.sessions.begin() as session:
            item = await session.get(RemnawaveServer, self.pid)
            self.assertTrue(
                await RemnawaveService(SQLRepository(session), self.settings).refresh(
                    item
                )
            )
        self.assertEqual((await self.snapshot())[0], 0)
        self.api.lose_response = True
        with self.assertRaises(ValueError):
            await self.issue(0)
        self.api.users.return_value = {"response": {"total": 1, "users": [{"id": 1}]}}
        async with self.db.sessions.begin() as session:
            item = await session.get(RemnawaveServer, self.pid)
            await RemnawaveService(SQLRepository(session), self.settings).refresh(item)
        self.assertEqual(await self.snapshot(), (0, ["reserved"], 0))

    async def test_balance_checkout_cannot_charge_when_slot_reserved(self):
        self.api.lose_response = True
        with self.assertRaises(ValueError):
            await self.issue(1)
        result = await self.checkout(None, 1, "capacity-balance-checkout")
        self.assertEqual(result.status_code, 409, result.text)
        async with self.db.sessions() as session:
            self.assertEqual(await SQLRepository(session).balance(self.uid), 100_000)
        self.assertEqual(len(self.api.creates), 1)

    async def test_trial_uses_same_admission_and_rolls_back_trial_flag(self):
        from app.services.screens import ScreenService

        self.api.creation_error = 503
        with self.assertRaises(ValueError):
            await self.issue(0)
        with self.assertRaises(ValueError):
            async with self.db.sessions.begin() as session:
                user = await session.get(User, self.other)
                await ScreenService(
                    SQLRepository(session), self.settings, None, None
                )._render("activate", user)
        async with self.db.sessions() as session:
            self.assertFalse((await session.get(User, self.other)).trial_used)
        self.assertEqual(len(self.api.creates), 1)

    async def test_telegram_admin_grant_cannot_bypass_reservation(self):
        from app.services.remnawave_bot import RemnawaveBot

        self.api.creation_error = 503
        with self.assertRaises(ValueError):
            await self.issue(0)
        with patch.object(RemnawaveService, "admin"):
            with self.assertRaises(ValueError):
                async with self.db.sessions.begin() as session:
                    repo = SQLRepository(session)
                    user = await session.get(User, self.uid)
                    await repo.set_setting(
                        f"vless_grant_intent:{user.id}:{self.pid}", "fixture"
                    )
                    await RemnawaveBot(repo, self.settings).input(
                        f"admin:r:input:grant:{self.pid}", user, "FO-2 | 30 | 1 | Test"
                    )
        self.assertEqual(len(self.api.creates), 1)
        self.assertEqual(await self.snapshot(), (0, ["reserved"], 0))

    async def test_migration_backfills_legacy_journal_and_is_repeatable(self):
        async with self.db.sessions.begin() as session:
            for index, (state, cid) in enumerate(
                (
                    ("associated", 900),
                    ("failed", None),
                    ("outcome_unknown", None),
                    ("remote_confirmed", None),
                )
            ):
                session.add(
                    VlessCreation(
                        id=str(index) * 64,
                        user_id=self.uid,
                        customer_ref=f"user:{self.uid}",
                        server_id=self.pid,
                        server_url="https://panel.example.test",
                        request=json.dumps({"fixture": index}),
                        state=state,
                        credential_id=cid,
                    )
                )
        async with self.db.engine.begin() as connection:
            await connection.run_sync(VlessReservation.__table__.drop)
            await connection.execute(
                text("ALTER TABLE remnawave_servers DROP COLUMN capacity_revision")
            )
        await upgrade(self.db.engine)
        await upgrade(self.db.engine)
        self.assertEqual(
            await self.snapshot(),
            (0, ["consumed", "released", "reserved", "reserved"], 0),
        )
        async with self.db.sessions() as session:
            self.assertEqual((await session.get(User, self.uid)).freeman_id, "FO-1")
            self.assertEqual(
                (await session.get(RemnawaveServer, self.pid)).capacity_revision, 0
            )
            rows = list(await session.scalars(select(VlessCreation)))
            self.assertEqual(
                {r.state for r in rows},
                {"associated", "failed", "outcome_unknown", "remote_confirmed"},
            )
        self.assertEqual(self.api.creates, [])
