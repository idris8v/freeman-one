"""I09 new VLESS placement only; all providers and payments are isolated mocks."""

import asyncio
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_billing_concurrency import BillingConcurrency
from test_remnawave_operations import Wire
from test_vless_creation import Panel

from app.database.models import (
    Payment,
    RemnawaveServer,
    Subscription,
    User,
    VlessCreation,
    VlessCredential,
    VlessReservation,
    VlessSync,
    now,
)
from app.database.repositories import SQLRepository
from app.database.vless_fallback_migration import upgrade
from app.services import vless_capacity, vless_creation, vless_fallback, vless_provision
from app.services.remnawave import RemnawaveApiClient, RemnawaveApiError
from app.services.vless_sync import VlessReconciler


class Provider(Panel):
    def __init__(self):
        super().__init__()
        self.missing_code = "A025"
        self.before_create = None
        self.lookup_faults = []

    async def user_by_username(self, username):
        if self.lookup_faults:
            fault = self.lookup_faults.pop(0)
            if fault:
                raise fault
        try:
            return await super().user_by_username(username)
        except RemnawaveApiError as error:
            if error.status == 404:
                raise RemnawaveApiError(
                    "Fixture missing user", 404, error_code=self.missing_code
                ) from error
            raise

    async def create_user(self, body):
        if self.before_create:
            await self.before_create()
        return await super().create_user(body)


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class VlessFallback(unittest.IsolatedAsyncioTestCase):
    checkout = BillingConcurrency.checkout
    paid_order = BillingConcurrency.paid_order
    receipt = BillingConcurrency.receipt

    async def asyncSetUp(self):
        await BillingConcurrency.asyncSetUp(self)
        self.db.sessions.configure(info={})
        self.apis = {}
        self.pids, self.sids = [], []
        async with self.db.sessions.begin() as session:
            for index in (1, 2):
                panel = RemnawaveServer(
                    name=f"Fixture {index}",
                    country="TEST",
                    api_url=f"https://panel{index}.example.test",
                    api_token="fixture",
                    internal_squad_uuid=f"squad-{index}",
                    priority=index * 10,
                    health="online",
                    max_users=2,
                )
                session.add(panel)
                await session.flush()
                self.pids.append(panel.id)
                self.apis[panel.id] = Provider()
            for uid in (self.uid, self.other):
                sub = Subscription(
                    user_id=uid,
                    kind="paid",
                    expires_at=now() + timedelta(days=30),
                    device_limit=3,
                )
                session.add(sub)
                await session.flush()
                self.sids.append(sub.id)
        provider = patch(
            "app.services.remnawave.RemnawaveService.api",
            side_effect=lambda p: self.apis[p.id],
        )
        provider.start()
        self.addCleanup(provider.stop)

    async def issue(self, index=0, explicit=False, rollback=False):
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            sub = await session.get(Subscription, self.sids[index])
            user = await session.get(User, sub.user_id)
            if explicit:
                result = await vless_provision.provision(
                    repo,
                    self.settings,
                    sub,
                    user,
                    await session.get(RemnawaveServer, self.pids[0]),
                    user,
                )
            else:
                result = await vless_provision.ensure(repo, self.settings, sub, user)
            if rollback:
                session.info["fail_fixture_local"] = True
            return result.id, result.server_id

    async def rows(self):
        async with self.db.sessions() as session:
            return (
                list(
                    await session.scalars(
                        select(VlessCreation).order_by(VlessCreation.id)
                    )
                ),
                list(
                    await session.scalars(
                        select(VlessReservation).order_by(VlessReservation.operation_id)
                    )
                ),
                list(
                    await session.scalars(
                        select(RemnawaveServer).order_by(RemnawaveServer.id)
                    )
                ),
                await session.scalar(select(func.count()).select_from(VlessCredential)),
            )

    async def test_confirmed_rejection_falls_back_without_duplicate_key(self):
        self.apis[self.pids[0]].creation_error = 401
        cid, pid = await self.issue()
        self.assertEqual(pid, self.pids[1])
        operations, reservations, panels, keys = await self.rows()
        self.assertEqual((len(operations), len(reservations), keys), (1, 1, 1))
        self.assertEqual(
            (operations[0].server_id, reservations[0].server_id), (pid, pid)
        )
        self.assertEqual([p.users_total for p in panels], [0, 1])
        self.assertEqual(sum(len(p.remote) for p in self.apis.values()), 1)
        self.assertEqual(await self.issue(), (cid, pid))

    async def test_ambiguous_timeout_remains_on_original_panel(self):
        first = self.apis[self.pids[0]]
        first.lose_response = True
        with self.assertRaises(ValueError):
            await self.issue()
        operations, reservations, panels, keys = await self.rows()
        self.assertEqual(
            (operations[0].server_id, reservations[0].state, keys),
            (self.pids[0], "reserved", 0),
        )
        self.assertEqual(self.apis[self.pids[1]].creates, [])
        first.lose_response = first.lookup_error = False
        self.assertEqual((await self.issue())[1], self.pids[0])
        self.assertEqual(len(first.creates), 1)

    async def test_gateway_404_does_not_release_a_dispatched_creation(self):
        first = self.apis[self.pids[0]]
        first.lookup_faults = [None, RemnawaveApiError("fixture gateway", 404)]
        first.creation_error = 401
        with self.assertRaises(ValueError):
            await self.issue()
        operations, reservations, panels, keys = await self.rows()
        self.assertEqual(reservations[0].state, "reserved")
        self.assertEqual(len(first.creates), 1)
        self.assertEqual(self.apis[self.pids[1]].creates, [])

    async def test_explicit_admin_panel_is_not_replaced(self):
        self.apis[self.pids[0]].creation_error = 401
        with self.assertRaises(ValueError):
            await self.issue(explicit=True)
        self.assertEqual(self.apis[self.pids[1]].creates, [])

    async def configure(self, pid, **fields):
        async with self.db.sessions.begin() as session:
            panel = await session.get(RemnawaveServer, pid)
            for key, value in fields.items():
                setattr(panel, key, value)

    async def test_lookup_outage_before_any_dispatch_safely_falls_back(self):
        primary = self.apis[self.pids[0]]
        primary.lookup_faults = [RemnawaveApiError("fixture timeout")]
        self.assertEqual((await self.issue())[1], self.pids[1])
        self.assertEqual(primary.creates, [])
        self.assertEqual(len(self.apis[self.pids[1]].creates), 1)
        self.assertEqual(sum(len(p.remote) for p in self.apis.values()), 1)

    async def test_later_rejected_retry_cannot_erase_an_uncertain_post(self):
        primary = self.apis[self.pids[0]]
        primary.creation_error = 503
        with self.assertRaises(ValueError):
            await self.issue()
        primary.creation_error = 401
        with self.assertRaises(ValueError):
            await self.issue()
        ops, reservations, _, _ = await self.rows()
        self.assertEqual(
            (ops[0].dispatch_state, reservations[0].state),
            ("outcome_unknown", "reserved"),
        )
        self.assertEqual(self.apis[self.pids[1]].creates, [])

    async def test_malformed_creation_response_never_moves_the_operation(self):
        primary = self.apis[self.pids[0]]
        primary.malformed_response = True
        with self.assertRaises(ValueError):
            await self.issue()
        self.assertEqual(self.apis[self.pids[1]].creates, [])
        primary.malformed_response = False
        self.assertEqual((await self.issue())[1], self.pids[0])
        self.assertEqual(len(primary.creates), 1)

    async def test_fallback_preserves_frozen_expiry_devices_and_traffic(self):
        async with self.db.sessions.begin() as session:
            await SQLRepository(session).set_setting(
                f"subscription_traffic_limit:{self.sids[0]}", str(11 * 1024**3)
            )
        self.apis[self.pids[0]].creation_error = 422
        await self.issue()
        first = self.apis[self.pids[0]].creates[0]
        second = self.apis[self.pids[1]].creates[0]
        self.assertEqual(
            {k: v for k, v in first.items() if k != "activeInternalSquads"},
            {k: v for k, v in second.items() if k != "activeInternalSquads"},
        )
        self.assertEqual(
            (second["hwidDeviceLimit"], second["trafficLimitBytes"]), (3, 11 * 1024**3)
        )
        self.assertEqual(second["activeInternalSquads"], ["squad-2"])

    async def test_ineligible_or_full_fallback_panels_are_never_called(self):
        primary = self.apis[self.pids[0]]
        primary.creation_error = 401
        for values in (
            dict(status="disabled"),
            dict(status="active", health="offline"),
            dict(health="online", internal_squad_uuid=""),
            dict(internal_squad_uuid="squad-2", users_total=2),
        ):
            await self.configure(self.pids[1], **values)
            with self.assertRaises(ValueError):
                await self.issue()
            self.assertEqual(self.apis[self.pids[1]].creates, [])
        await self.configure(self.pids[1], users_total=1)
        self.assertEqual((await self.issue())[1], self.pids[1])

    async def compete(self, winner):
        primary, backup = (self.apis[p] for p in self.pids)
        primary.creation_error = 401
        await self.configure(self.pids[1], max_users=1)
        backup.entered, backup.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.issue(winner))
        try:
            await asyncio.wait_for(backup.entered.wait(), 10)
            with self.assertRaises(ValueError):
                await asyncio.wait_for(self.issue(1 - winner), 10)
            async with self.db.sessions.begin() as session:
                for model in (RemnawaveServer, VlessCreation, VlessReservation):
                    self.assertIsNotNone(
                        await session.scalar(select(model).with_for_update(nowait=True))
                    )
        finally:
            backup.release.set()
        self.assertEqual((await asyncio.wait_for(task, 10))[1], self.pids[1])
        _, reservations, panels, keys = await self.rows()
        self.assertEqual((panels[1].users_total, keys, len(backup.remote)), (1, 1, 1))
        self.assertEqual(
            sorted(r.state for r in reservations), ["consumed", "released"]
        )

    async def test_two_clients_compete_for_final_fallback_slot(self):
        await self.compete(0)

    async def test_fallback_contention_reverse_execution_order(self):
        await self.compete(1)

    async def test_same_operation_concurrent_fallback_associates_once(self):
        primary = self.apis[self.pids[0]]
        primary.creation_error = 401
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait():
            entered.set()
            await asyncio.wait_for(release.wait(), 10)

        primary.before_create = wait
        a = asyncio.create_task(self.issue())
        b = None
        try:
            await asyncio.wait_for(entered.wait(), 10)
            b = asyncio.create_task(self.issue())
            await asyncio.sleep(0.2)
        finally:
            release.set()
        try:
            results = await asyncio.wait_for(asyncio.gather(a, b), 20)
            self.assertEqual(results[0], results[1])
            # Confirmed rejections may be retried on the same stable identity;
            # count actual remote keys, not rejected POST attempts.
            self.assertEqual(len(self.apis[self.pids[1]].creates), 1)
            self.assertEqual([len(self.apis[p].remote) for p in self.pids], [0, 1])
            self.assertEqual((await self.rows())[-1], 1)
        finally:
            for task in (a, b):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (a, b) if t), return_exceptions=True)

    async def test_reclaimed_rejection_waits_for_same_operation_recovery(self):
        entered, release = asyncio.Event(), asyncio.Event()
        second, release_second = asyncio.Event(), asyncio.Event()
        waiting, moving, observed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        tasks, a, b = [], None, None
        original_move = vless_fallback.move
        original_recorded = vless_creation.recorded
        original_claim = vless_capacity.claim

        async def before_create():
            if asyncio.current_task() is a:
                entered.set()
                await release.wait()
            else:
                second.set()
                await release_second.wait()

        async def claim(*args, **kwargs):
            result = await original_claim(*args, **kwargs)
            if asyncio.current_task() is b and result is None:
                waiting.set()
            return result

        async def move(*args, **kwargs):
            if asyncio.current_task() is a and not moving.is_set():
                await asyncio.wait_for(second.wait(), 10)
                result = await original_move(*args, **kwargs)
                self.assertIsNone(result)  # B owns the same original placement.
                moving.set()
                return result
            return await original_move(*args, **kwargs)

        async def recorded(*args, **kwargs):
            result = await original_recorded(*args, **kwargs)
            if (
                asyncio.current_task() is a
                and moving.is_set()
                and result.state != "failed"
            ):
                observed.set()
            return result

        primary, backup = (self.apis[p] for p in self.pids)
        primary.creation_error, primary.before_create = 401, before_create
        try:
            with (
                patch.object(vless_fallback, "move", side_effect=move),
                patch.object(vless_creation, "recorded", side_effect=recorded),
                patch.object(vless_capacity, "claim", side_effect=claim),
            ):
                a = asyncio.create_task(self.issue())
                tasks.append(a)
                await asyncio.wait_for(entered.wait(), 10)
                b = asyncio.create_task(self.issue())
                tasks.append(b)
                await asyncio.wait_for(waiting.wait(), 10)
                release.set()
                waiter = asyncio.create_task(observed.wait())
                tasks.append(waiter)
                done, _ = await asyncio.wait(
                    (a, waiter), timeout=15, return_when=asyncio.FIRST_COMPLETED
                )
                if a in done:
                    await a  # Original defect raises NotCreated here.
                self.assertTrue(observed.is_set())
                release_second.set()
                results = await asyncio.wait_for(asyncio.gather(a, b), 20)
                self.assertEqual(results[0], results[1])
                self.assertEqual([len(primary.creates), len(backup.creates)], [2, 1])
                self.assertEqual([len(primary.remote), len(backup.remote)], [0, 1])
                self.assertEqual((await self.rows())[-1], 1)
        finally:
            release.set()
            release_second.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_stale_worker_cannot_dispatch_after_safe_rebinding(self):
        primary = self.apis[self.pids[0]]
        primary.creation_error = 401
        entered, release = asyncio.Event(), asyncio.Event()
        original = vless_fallback.dispatch
        old = None

        async def dispatch(repo, identity, claim):
            if asyncio.current_task() is old:
                entered.set()
                await asyncio.wait_for(release.wait(), 15)
            return await original(repo, identity, claim)

        with patch("app.services.vless_fallback.dispatch", side_effect=dispatch):
            old = asyncio.create_task(self.issue())
            try:
                await asyncio.wait_for(entered.wait(), 10)
                async with self.db.sessions.begin() as session:
                    row = await session.scalar(select(VlessReservation))
                    row.lease_until = now() - timedelta(seconds=1)
                primary.lookup_faults = [
                    RemnawaveApiError("fixture lookup outage", 503)
                ]
                winner = await asyncio.wait_for(self.issue(), 10)
            finally:
                release.set()
            with self.assertRaises(ValueError):
                await old
        self.assertEqual(winner[1], self.pids[1])
        self.assertEqual([len(self.apis[p].creates) for p in self.pids], [0, 1])
        self.assertEqual((await self.rows())[-1], 1)

    async def test_restart_after_rebinding_before_dispatch_uses_new_panel(self):
        self.apis[self.pids[0]].creation_error = 401
        from app.services import vless_creation

        confirm = vless_creation.confirm_remote

        async def interrupted(*args, **kwargs):
            if args[5].id == self.pids[1]:
                raise asyncio.CancelledError()
            return await confirm(*args, **kwargs)

        with (
            patch(
                "app.services.vless_creation.confirm_remote", side_effect=interrupted
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.issue()
        ops, res, _, keys = await self.rows()
        self.assertEqual(
            (ops[0].server_id, ops[0].dispatch_state, res[0].state, keys),
            (self.pids[1], "not_started", "reserved", 0),
        )
        await self.db.engine.dispose()
        self.assertEqual((await self.issue())[1], self.pids[1])
        self.assertEqual([len(self.apis[p].creates) for p in self.pids], [1, 1])

    async def test_fallback_commit_failure_keeps_original_failed_binding(self):
        self.apis[self.pids[0]].creation_error = 401
        armed = True

        def fail(session):
            nonlocal armed
            if armed and any(
                isinstance(r, VlessCreation)
                and r.server_id == self.pids[1]
                and r.dispatch_state == "not_started"
                for r in session.dirty
            ):
                armed = False
                raise SQLAlchemyError("fixture fallback commit")

        event.listen(Session, "before_commit", fail)
        try:
            with self.assertRaises(ValueError):
                await self.issue()
        finally:
            event.remove(Session, "before_commit", fail)
        self.assertFalse(armed)
        ops, res, panels, _ = await self.rows()
        self.assertEqual(
            (ops[0].server_id, res[0].server_id, res[0].state),
            (self.pids[0], self.pids[0], "released"),
        )
        self.assertEqual([p.users_total for p in panels], [0, 0])
        self.assertEqual(self.apis[self.pids[1]].creates, [])
        self.assertEqual((await self.issue())[1], self.pids[1])

    async def test_confirmed_fallback_survives_local_association_rollback(self):
        self.apis[self.pids[0]].creation_error = 401
        armed = True

        def fail(session):
            nonlocal armed
            if session.info.pop("fail_fixture_local", False):
                armed = False
                raise SQLAlchemyError("fixture local commit")

        event.listen(Session, "before_commit", fail)
        try:
            with self.assertRaises(SQLAlchemyError):
                await self.issue(rollback=True)
        finally:
            event.remove(Session, "before_commit", fail)
        self.assertFalse(armed)
        ops, res, panels, keys = await self.rows()
        self.assertEqual(
            (ops[0].server_id, res[0].state, keys), (self.pids[1], "consumed", 0)
        )
        self.assertEqual([p.users_total for p in panels], [0, 1])
        await self.db.engine.dispose()
        self.assertEqual((await self.issue())[1], self.pids[1])
        self.assertEqual([len(self.apis[p].creates) for p in self.pids], [1, 1])

    async def test_balance_purchase_rejected_without_any_safe_panel_has_no_charge(self):
        self.apis[self.pids[0]].creation_error = 401
        await self.configure(self.pids[1], max_users=0)
        response = await self.checkout(None, 1, "i09-unpaid-fallback")
        self.assertEqual(response.status_code, 409, response.text)
        async with self.db.sessions() as session:
            self.assertEqual(await SQLRepository(session).balance(self.uid), 100_000)
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Payment)), 0
            )
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(VlessCredential)),
                0,
            )

    async def test_balance_purchase_fallback_and_duplicate_request_charge_once(self):
        self.apis[self.pids[0]].creation_error = 401
        first = await self.checkout(None, 1, "i09-balance-fallback")
        self.assertEqual(first.status_code, 200, first.text)
        duplicate = await self.checkout(None, 1, "i09-balance-fallback")
        self.assertEqual(first.json(), duplicate.json())
        async with self.db.sessions() as session:
            self.assertEqual(await SQLRepository(session).balance(self.uid), 90_000)
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Payment)), 1
            )
            self.assertEqual(
                (await session.scalar(select(VlessCredential))).server_id, self.pids[1]
            )

    async def test_paid_order_stays_pending_then_recovers_without_another_charge(self):
        self.apis[self.pids[0]].creation_error = 401
        await self.configure(self.pids[1], max_users=0)
        order = await self.paid_order(0, 1)
        with self.assertRaises(ValueError):
            await self.receipt(order)
        async with self.db.sessions() as session:
            payment = await session.get(Payment, order[0])
            self.assertEqual(payment.status, "order_pending")
            self.assertEqual(await SQLRepository(session).balance(self.uid), 100_000)
        await self.configure(self.pids[1], max_users=1)
        await self.receipt(order)
        await self.receipt(order)
        async with self.db.sessions() as session:
            payment = await session.get(Payment, order[0])
            self.assertEqual(payment.status, "order_paid")
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Payment)), 1
            )
            self.assertEqual(await SQLRepository(session).balance(self.uid), 100_000)
            self.assertEqual(
                (await session.scalar(select(VlessCredential))).server_id, self.pids[1]
            )

    async def test_paid_timeout_recovers_only_on_original_panel_after_restart(self):
        primary = self.apis[self.pids[0]]
        primary.lose_response = True
        order = await self.paid_order(0, 1)
        with self.assertRaises(ValueError):
            await self.receipt(order)
        await self.db.engine.dispose()
        primary.lose_response = primary.lookup_error = False
        await self.receipt(order)
        await self.receipt(order)
        async with self.db.sessions() as session:
            self.assertEqual(
                (await session.scalar(select(VlessCredential))).server_id, self.pids[0]
            )
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Payment)), 1
            )
        self.assertEqual(self.apis[self.pids[1]].creates, [])
        self.assertEqual(len(primary.creates), 1)

    async def test_fallback_never_moves_an_existing_customer_key(self):
        cid, pid = await self.issue(0)
        self.apis[self.pids[0]].creation_error = 401
        await self.issue(1)
        async with self.db.sessions() as session:
            old = await session.get(VlessCredential, cid)
            self.assertEqual(old.server_id, pid)
            self.assertEqual(old.remote_user_uuid, "100")
            self.assertEqual(old.subscription_url, "https://sub.example.test/fixture")
        self.assertEqual(sum(len(p.remote) for p in self.apis.values()), 2)

    async def test_all_failures_do_not_cycle_within_one_request(self):
        for provider in self.apis.values():
            provider.creation_error = 401
        with self.assertRaises(ValueError):
            await self.issue()
        self.assertEqual([len(self.apis[p].creates) for p in self.pids], [1, 1])
        with self.assertRaises(ValueError):
            await self.issue()
        self.assertEqual([len(self.apis[p].creates) for p in self.pids], [2, 2])

    async def test_later_request_can_use_a_previously_rejected_panel_after_recovery(
        self,
    ):
        for provider in self.apis.values():
            provider.creation_error = 401
        with self.assertRaises(ValueError):
            await self.issue()
        self.apis[self.pids[0]].creation_error = None
        self.assertEqual((await self.issue())[1], self.pids[0])
        self.assertEqual(sum(len(p.remote) for p in self.apis.values()), 1)

    async def test_legacy_operation_with_no_provenance_stays_pinned(self):
        self.apis[self.pids[0]].creation_error = 401
        with self.assertRaises(ValueError):
            await self.issue(explicit=True)
        async with self.db.sessions.begin() as session:
            op = await session.scalar(select(VlessCreation))
            op.dispatch_state, op.placement = "legacy_unknown", "{}"
        with self.assertRaises(ValueError):
            await self.issue()
        self.assertEqual(self.apis[self.pids[1]].creates, [])
        self.assertEqual((await self.rows())[1][0].state, "reserved")

    async def test_migration_is_repeatable_preserves_legacy_keys_and_does_not_guess(
        self,
    ):
        cid, pid = await self.issue()
        async with self.db.engine.begin() as connection:
            await connection.exec_driver_sql(
                "ALTER TABLE vless_creations DROP COLUMN dispatch_state"
            )
            await connection.exec_driver_sql(
                "ALTER TABLE vless_creations DROP COLUMN placement"
            )
        await upgrade(self.db.engine)
        await upgrade(self.db.engine)
        async with self.db.sessions() as session:
            op = await session.scalar(select(VlessCreation))
            self.assertEqual(
                (op.dispatch_state, op.placement, op.credential_id),
                ("legacy_unknown", "{}", cid),
            )
        self.assertEqual(await self.issue(), (cid, pid))

    async def test_stale_reconciliation_observations_are_discarded_after_fallback(self):
        primary, backup = (self.apis[p] for p in self.pids)
        primary.creation_error, backup.creation_error = 401, 503
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait():
            entered.set()
            await asyncio.wait_for(release.wait(), 15)

        primary.before_create = wait
        task = asyncio.create_task(self.issue())
        reconciler = VlessReconciler(self.db, self.settings, rate_seconds=0)
        try:
            await asyncio.wait_for(entered.wait(), 10)
            await reconciler.seed()
            work = await reconciler.claim()
            self.assertEqual(work.panel.id, self.pids[0])
        finally:
            release.set()
        with self.assertRaises(ValueError):
            await task
        await reconciler.finish(work, "missing", {"exists": False})
        async with self.db.sessions() as session:
            row = await session.get(VlessSync, work.id)
            self.assertEqual((row.outcome, row.observed), ("local_changed", None))
        await reconciler.seed()
        async with self.db.sessions() as session:
            row = await session.get(VlessSync, work.id)
            self.assertEqual(
                (row.server_id, row.outcome, row.observed),
                (self.pids[1], "pending", None),
            )
        work = await reconciler.claim()
        self.assertEqual(work.panel.id, self.pids[1])
        await reconciler.process(work)
        self.assertEqual(len(backup.creates), 1)  # Only the checkout dispatched POST.
        self.assertEqual((await self.rows())[1][0].state, "reserved")

    async def test_retired_creation_sync_is_rearmed_for_new_panel(self):
        self.apis[self.pids[0]].creation_error = 401
        await self.configure(self.pids[1], max_users=0)
        with self.assertRaises(ValueError):
            await self.issue()
        ops, _, _, _ = await self.rows()
        async with self.db.sessions.begin() as session:
            session.add(
                VlessSync(
                    id="creation:" + ops[0].id,
                    creation_id=ops[0].id,
                    server_id=self.pids[0],
                    outcome="retired",
                    observed='{"exists":false}',
                )
            )
        await self.configure(self.pids[1], max_users=1)
        self.apis[self.pids[1]].creation_error = 503
        with self.assertRaises(ValueError):
            await self.issue()
        await VlessReconciler(self.db, self.settings).seed()
        async with self.db.sessions() as session:
            row = await session.get(VlessSync, "creation:" + ops[0].id)
            self.assertEqual(
                (row.server_id, row.outcome, row.observed),
                (self.pids[1], "pending", None),
            )

    async def test_verified_failure_can_fallback_after_original_panel_is_removed(self):
        self.apis[self.pids[0]].creation_error = 401
        await self.configure(self.pids[1], max_users=0)
        with self.assertRaises(ValueError):
            await self.issue()
        async with self.db.sessions.begin() as session:
            await session.delete(await session.get(RemnawaveServer, self.pids[0]))
        await self.configure(self.pids[1], max_users=1)
        self.assertEqual((await self.issue())[1], self.pids[1])

    async def test_concurrent_rebinding_is_followed_instead_of_retrying_old_panel(self):
        self.apis[self.pids[0]].creation_error = 401
        original = vless_fallback.move
        armed = True

        async def raced(repo, identity, source, **kwargs):
            nonlocal armed
            alternative = await original(repo, identity, source, **kwargs)
            if armed and alternative:
                armed = False
                # A peer committed this binding before this caller acquired it.
                raise vless_fallback.PlacementChanged("fixture concurrent move")
            return alternative

        with patch("app.services.vless_fallback.move", side_effect=raced):
            self.assertEqual((await self.issue())[1], self.pids[1])
        self.assertFalse(armed)
        self.assertEqual([len(self.apis[p].creates) for p in self.pids], [1, 1])

    async def test_stale_failed_snapshot_follows_completed_concurrent_rebinding(self):
        """Pause after the failed journal read, then let another worker move it."""
        primary = self.apis[self.pids[0]]
        primary.creation_error = 401
        async with self.db.sessions.begin() as session:
            second = await session.get(RemnawaveServer, self.pids[1])
            second.health = "offline"
        with self.assertRaises(ValueError):
            await self.issue()
        async with self.db.sessions.begin() as session:
            second = await session.get(RemnawaveServer, self.pids[1])
            second.health = "online"
        read, release = asyncio.Event(), asyncio.Event()
        original = vless_creation.recorded
        paused = None
        once = True

        async def recorded(repo, identity):
            nonlocal once
            snapshot = await original(repo, identity)
            if (
                asyncio.current_task() is paused
                and once
                and snapshot
                and snapshot.state == "failed"
            ):
                once = False
                read.set()
                await release.wait()
            return snapshot

        with patch.object(vless_creation, "recorded", side_effect=recorded):
            paused = asyncio.create_task(self.issue())
            try:
                await asyncio.wait_for(read.wait(), 10)
                winner = await self.issue()
                release.set()
                self.assertEqual(await asyncio.wait_for(paused, 15), winner)
            finally:
                release.set()
                if not paused.done():
                    paused.cancel()
                await asyncio.gather(paused, return_exceptions=True)
        self.assertEqual([len(self.apis[p].remote) for p in self.pids], [0, 1])
        self.assertEqual((await self.rows())[-1], 1)


class FallbackApiContract(unittest.IsolatedAsyncioTestCase):
    async def test_only_documented_user_missing_errors_confirm_absence(self):
        for code, valid in [
            ("A025", True),
            ("A063", True),
            ("A007", False),
            (None, False),
        ]:
            wire = Wire((404, {"errorCode": code}))
            with (
                patch(
                    "app.services.remnawave.aiohttp.ClientSession", return_value=wire
                ),
                self.assertRaises(RemnawaveApiError) as caught,
            ):
                await RemnawaveApiClient(
                    "https://panel.example.test", "fixture"
                ).user_by_username("fo_fixture")
            self.assertEqual(vless_fallback.confirmed_absence(caught.exception), valid)

    async def test_http_200_negative_and_html_404_are_not_absence(self):
        for response in [
            (200, {"response": {"isSuccess": False}}),
            (404, ValueError("HTML")),
        ]:
            wire = Wire(response)
            with (
                patch(
                    "app.services.remnawave.aiohttp.ClientSession", return_value=wire
                ),
                self.assertRaises(RemnawaveApiError) as caught,
            ):
                await RemnawaveApiClient(
                    "https://panel.example.test", "fixture"
                ).user_by_username("fo_fixture")
            self.assertFalse(vless_fallback.confirmed_absence(caught.exception))

    async def test_rejection_requires_pristine_dispatch_and_authoritative_absence(self):
        missing = RemnawaveApiError("missing", 404, error_code="A025")
        for status in (0, 200, 400, 401, 403, 404, 409, 422, 429, 500, 503):
            self.assertEqual(
                vless_fallback.safe_rejection(
                    SimpleNamespace(pristine=True),
                    RemnawaveApiError("fixture", status),
                    missing,
                ),
                status in {400, 401, 403, 422},
            )
            self.assertFalse(
                vless_fallback.safe_rejection(
                    SimpleNamespace(pristine=False),
                    RemnawaveApiError("fixture", status),
                    missing,
                )
            )
        self.assertFalse(
            vless_fallback.safe_rejection(
                SimpleNamespace(pristine=True),
                RemnawaveApiError("fixture", 401),
                RemnawaveApiError("gateway", 404),
            )
        )
