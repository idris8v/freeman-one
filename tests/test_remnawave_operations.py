"""I08: real adapter over isolated HTTP fixtures; no live provider requests."""

import copy
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from sqlalchemy import select
from test_billing_concurrency import BillingConcurrency

from app.database.models import AccessKey, Device, Subscription, User, VlessCredential
from app.database.repositories import SQLRepository
from app.services import vless_provision
from app.services.remnawave import RemnawaveApiClient, RemnawaveApiError
from app.services.remnawave_bot import RemnawaveBot
from app.services.vpn import VpnService

REAL_CONTROL = vless_provision.control
UUID = "5c9d8b73-5d03-4c91-89ef-232a40402ff0"


def user(**values):
    return {
        "response": dict(
            id=123,
            status="ACTIVE",
            hwidDeviceLimit=1,
            trafficLimitBytes=0,
            trafficLimitStrategy="NO_RESET",
            expireAt="2027-03-01T12:00:00Z",
            userTraffic={"usedTrafficBytes": 0},
            subscriptionUrl="https://sub.example.test/new",
            shortUuid="new-short",
        )
        | values
    }


class Wire:
    """The actual aiohttp adapter sees status, JSON, and network exceptions."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, urlsplit(url).path, kwargs.get("json")))
        if not self.responses:
            raise AssertionError("Unexpected HTTP call")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        status, payload = value

        class Response:
            headers = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def json(self, **_):
                if isinstance(payload, Exception):
                    raise payload
                return copy.deepcopy(payload)

        response = Response()
        response.status = status
        return response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


def api():
    return RemnawaveApiClient("https://panel.example.test", "fixture-only")


class OperationContract(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, method, args, response):
        wire = Wire(response)
        with patch("app.services.remnawave.aiohttp.ClientSession", return_value=wire):
            result = await getattr(api(), method)(*args)
        return result, wire.calls

    async def test_numeric_ids_are_used_in_installed_contract_not_vless_uuid(self):
        result, calls = await self.invoke(
            "update_user",
            ({"id": 123, "hwidDeviceLimit": 1},),
            (200, {"response": {**user()["response"], "vlessUuid": UUID}}),
        )
        self.assertEqual(
            calls, [("PATCH", "/api/users", {"id": 123, "hwidDeviceLimit": 1})]
        )
        self.assertEqual(result["response"]["id"], 123)

    async def test_delete_344_no_content_is_success(self):
        _, calls = await self.invoke("delete_user", ("123",), (204, None))
        self.assertEqual(calls, [("DELETE", "/api/users/123", None)])

    async def test_legacy_delete_requires_true_boolean(self):
        await self.invoke(
            "delete_user", (UUID,), (200, {"response": {"isDeleted": True}})
        )
        for value in (False, None, 0, 1, "true", "false"):
            with self.subTest(value=value), self.assertRaises(RemnawaveApiError):
                await self.invoke(
                    "delete_user", ("123",), (200, {"response": {"isDeleted": value}})
                )

    async def test_ambiguous_delete_is_not_success(self):
        for body in (
            {},
            {"response": {}},
            {"response": None},
            {"response": True},
            {"success": True},
        ):
            with self.subTest(body=body), self.assertRaises(RemnawaveApiError):
                await self.invoke("delete_user", ("123",), (200, body))

    async def test_explicit_negative_results_override_plausible_user_state(self):
        cases = [
            ("disable_user", ("123",), user(status="DISABLED")),
            ("enable_user", ("123",), user()),
            ("reset_traffic", ("123",), user()),
            ("update_user", ({"id": 123, "hwidDeviceLimit": 1},), user()),
        ]
        for method, args, body in cases:
            for location in (body, body["response"]):
                location["isSuccess"] = False
                with self.subTest(method=method), self.assertRaises(RemnawaveApiError):
                    await self.invoke(method, args, (200, body))
                del location["isSuccess"]

    async def test_enable_disable_need_matching_identity_and_status(self):
        for method, status in (("enable_user", "ACTIVE"), ("disable_user", "DISABLED")):
            await self.invoke(method, ("123",), (200, user(status=status)))
            for body in (
                {},
                user(id=456, status=status),
                user(id=True, status=status),
                user(status="EXPIRED"),
            ):
                with (
                    self.subTest(method=method, body=body),
                    self.assertRaises(RemnawaveApiError),
                ):
                    await self.invoke(method, ("123",), (200, body))

    async def test_patch_checks_every_requested_term_including_zero(self):
        body = {
            "id": 123,
            "hwidDeviceLimit": 0,
            "trafficLimitBytes": 0,
            "trafficLimitStrategy": "NO_RESET",
            "expireAt": "2027-03-01T15:00:00+03:00",
        }
        await self.invoke("update_user", (body,), (200, user(hwidDeviceLimit=0)))
        for field, bad in (
            ("hwidDeviceLimit", None),
            ("hwidDeviceLimit", False),
            ("trafficLimitBytes", 7),
            ("trafficLimitStrategy", "DAY"),
            ("expireAt", "2027-03-02T12:00:00Z"),
        ):
            with (
                self.subTest(field=field, bad=bad),
                self.assertRaises(RemnawaveApiError),
            ):
                response = user(hwidDeviceLimit=0)
                response["response"][field] = bad
                await self.invoke("update_user", (body,), (200, response))

    async def test_patch_respects_provider_millisecond_date_precision(self):
        body = {"id": 123, "expireAt": "2027-03-01T12:00:00.123456+00:00"}
        await self.invoke(
            "update_user", (body,), (200, user(expireAt="2027-03-01T12:00:00.123Z"))
        )
        with self.assertRaises(RemnawaveApiError):
            await self.invoke(
                "update_user", (body,), (200, user(expireAt="2027-03-01T12:00:00.125Z"))
            )

    async def test_patch_missing_fields_and_non_object_responses_are_ambiguous(self):
        for body in (
            {},
            {"response": {}},
            {"response": {"id": 123}},
            {"response": []},
            [],
        ):
            with self.subTest(body=body), self.assertRaises(RemnawaveApiError):
                await self.invoke(
                    "update_user", ({"id": 123, "hwidDeviceLimit": 2},), (200, body)
                )

    async def test_explicit_null_update_still_requires_returned_field(self):
        body = {"id": 123, "description": None}
        await self.invoke("update_user", (body,), (200, user(description=None)))
        with self.assertRaises(RemnawaveApiError):
            await self.invoke("update_user", (body,), (200, user()))

    async def test_traffic_reset_requires_confirmed_zero_for_correct_user(self):
        await self.invoke("reset_traffic", ("123",), (200, user()))
        for body in (
            {},
            user(id=456),
            user(userTraffic={}),
            user(userTraffic={"usedTrafficBytes": False}),
            user(userTraffic={"usedTrafficBytes": "0"}),
            user(userTraffic={"usedTrafficBytes": 9}),
        ):
            with self.subTest(body=body), self.assertRaises(RemnawaveApiError):
                await self.invoke("reset_traffic", ("123",), (200, body))

    async def test_hwid_mutations_confirm_complete_post_action_snapshot(self):
        from test_vless_devices import hwid, snapshot

        _, calls = await self.invoke(
            "delete_device", ("a", "123"), (200, snapshot(hwid("b")))
        )
        self.assertEqual(
            calls, [("POST", "/api/hwid/devices/delete", {"userId": 123, "hwid": "a"})]
        )
        await self.invoke("delete_all_devices", ("123",), (200, snapshot()))
        for method, args, body in (
            ("delete_device", ("a", "123"), snapshot(hwid("a"))),
            ("delete_all_devices", ("123",), snapshot(hwid())),
            ("delete_all_devices", ("123",), {"response": {"devices": []}}),
            ("delete_all_devices", ("123",), {"response": {"isSuccess": True}}),
        ):
            with (
                self.subTest(method=method, body=body),
                self.assertRaises(RemnawaveApiError),
            ):
                await self.invoke(method, args, (200, body))

    async def test_invalid_identifiers_and_payloads_never_reach_http(self):
        for method, args in [
            ("delete_user", (x,))
            for x in ("../123", "123/actions/enable", "", "0", -1, True)
        ] + [
            ("delete_device", ("a",)),
            ("update_user", ({"id": True, "hwidDeviceLimit": 1},)),
            ("update_user", ({"id": 123, "uuid": UUID},)),
            ("update_user", ({"id": 123, "hwidDeviceLimit": False},)),
        ]:
            wire = Wire()
            with (
                self.subTest(method=method, args=args),
                patch(
                    "app.services.remnawave.aiohttp.ClientSession", return_value=wire
                ),
                self.assertRaises(RemnawaveApiError),
            ):
                await getattr(api(), method)(*args)
            self.assertEqual(wire.calls, [])

    async def test_read_identities_use_the_same_validation_as_mutations(self):
        for method in ("user", "devices"):
            for value in ("123/../456", "", True):
                wire = Wire()
                with (
                    self.subTest(method=method, value=value),
                    patch(
                        "app.services.remnawave.aiohttp.ClientSession",
                        return_value=wire,
                    ),
                    self.assertRaises(RemnawaveApiError),
                ):
                    await getattr(api(), method)(value)
                self.assertEqual(wire.calls, [])
        _, calls = await self.invoke("user", ("123",), (200, user()))
        self.assertEqual(calls, [("GET", "/api/users/123", None)])

    async def test_legacy_uuid_remains_uuid_not_integer_or_vless_uuid(self):
        body = {"response": {"uuid": UUID, "status": "DISABLED", "hwidDeviceLimit": 2}}
        await self.invoke("disable_user", (UUID,), (200, body))
        _, calls = await self.invoke(
            "update_user", ({"uuid": UUID, "hwidDeviceLimit": 2},), (200, body)
        )
        self.assertEqual(calls[0][2], {"uuid": UUID, "hwidDeviceLimit": 2})

    async def test_revoke_validates_user_and_url_before_caller_replaces_key(self):
        _, calls = await self.invoke("revoke_user", ("123",), (200, user()))
        self.assertEqual(
            calls,
            [("POST", "/api/users/123/actions/revoke", {"revokeOnlyPasswords": False})],
        )
        for body in (
            user(id=456),
            user(subscriptionUrl="javascript:bad"),
            user(subscriptionUrl="https://[malformed"),
            user(shortUuid=""),
            {"response": {"isSuccess": False}},
        ):
            with self.subTest(body=body), self.assertRaises(RemnawaveApiError):
                await self.invoke("revoke_user", ("123",), (200, body))

    async def test_error_envelope_with_http_200_is_never_success(self):
        for body in (
            {"errorCode": "A001", **user()},
            {"response": {**user()["response"], "error": "rejected"}},
        ):
            with self.subTest(body=body), self.assertRaises(RemnawaveApiError):
                await self.invoke("enable_user", ("123",), (200, body))

    async def test_timeouts_invalid_json_and_204_mutations_remain_unconfirmed(self):
        for response in (TimeoutError(), (200, ValueError("bad JSON")), (204, None)):
            with self.subTest(response=response), self.assertRaises(RemnawaveApiError):
                await self.invoke("disable_user", ("123",), response)


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class OperationPostgres(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await BillingConcurrency.asyncSetUp(self)
        self.sid, self.kid, self.expiry = await BillingConcurrency.subscription(self)
        async with self.db.sessions.begin() as session:
            c = await session.scalar(select(VlessCredential))
            c.remote_user_uuid = "123"
            c.traffic_used = 8192
            self.cid = c.id
            session.add(
                Device(
                    subscription_id=self.sid,
                    name="phone",
                    type="phone",
                    status="active",
                    provisioning_id="a",
                )
            )
        self.adapter = api()
        for p in (
            patch("app.services.vless_provision.control", new=REAL_CONTROL),
            patch(
                "app.services.remnawave.RemnawaveService.api", return_value=self.adapter
            ),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def local(self):
        async with self.db.sessions() as session:
            c = await session.get(VlessCredential, self.cid)
            k = await session.get(AccessKey, self.kid)
            sub = await session.get(Subscription, self.sid)
            devices = list(await session.scalars(select(Device.status)))
            return (
                c.status,
                k.status,
                c.traffic_used,
                c.device_limit,
                sub.device_limit,
                c.last_sync,
                sub.expires_at,
                devices,
            )

    async def control(self, action):
        async with self.db.sessions.begin() as session:
            await VpnService(
                SQLRepository(session), self.settings
            ).set_subscription_status(self.sid, action)

    async def test_negative_delete_never_marks_local_key_deleted(self):
        before = await self.local()
        wire = Wire((200, {"response": {"isDeleted": False}}))
        with (
            patch("app.services.remnawave.aiohttp.ClientSession", return_value=wire),
            self.assertRaises(ValueError),
        ):
            await self.control("deleted")
        self.assertEqual(await self.local(), before)

    async def test_confirmed_204_delete_marks_credential_deleted(self):
        with patch(
            "app.services.remnawave.aiohttp.ClientSession",
            return_value=Wire((204, None)),
        ):
            await self.control("deleted")
        self.assertEqual((await self.local())[0], "deleted")

    async def test_ambiguous_delete_preserves_state(self):
        before = await self.local()
        for body in ({}, {"response": {}}, {"response": {"isDeleted": "true"}}):
            with (
                patch(
                    "app.services.remnawave.aiohttp.ClientSession",
                    return_value=Wire((200, body)),
                ),
                self.assertRaises(ValueError),
            ):
                await self.control("deleted")
        self.assertEqual(await self.local(), before)

    async def test_gateway_404_is_not_confirmed_deletion(self):
        before = await self.local()
        wire = Wire((404, {}), (404, {}))
        with (
            patch("app.services.remnawave.aiohttp.ClientSession", return_value=wire),
            self.assertRaises(ValueError),
        ):
            await self.control("deleted")
        self.assertEqual(await self.local(), before)

    async def test_documented_missing_user_allows_idempotent_deletion(self):
        wire = Wire((404, {"errorCode": "A025"}), (404, {"errorCode": "A063"}))
        with patch("app.services.remnawave.aiohttp.ClientSession", return_value=wire):
            await self.control("deleted")
        self.assertEqual((await self.local())[0], "deleted")

    async def test_negative_disable_and_timeout_do_not_change_state(self):
        before = await self.local()
        for response in (
            (200, user(status="ACTIVE")),
            (200, {"response": {"isSuccess": False}}),
            TimeoutError(),
        ):
            with (
                patch(
                    "app.services.remnawave.aiohttp.ClientSession",
                    return_value=Wire(response),
                ),
                self.assertRaises(ValueError),
            ):
                await self.control("disabled")
        self.assertEqual(await self.local(), before)

    async def test_confirmed_disable_updates_local_state(self):
        with patch(
            "app.services.remnawave.aiohttp.ClientSession",
            return_value=Wire((200, user(status="DISABLED"))),
        ):
            await self.control("disabled")
        self.assertEqual((await self.local())[0], "disabled")

    async def bot_action(self, operation):
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            actor = await session.get(User, self.uid)
            bot = RemnawaveBot(repo, self.settings)
            await repo.set_setting(
                "vless_confirm:test",
                json.dumps(
                    dict(
                        actor=actor.telegram_id,
                        cid=self.cid,
                        operation=operation,
                        expires=9999999999,
                        status="active",
                    )
                ),
            )
            original = bot.render

            async def render(action, user):
                if action == "admin:r:apply:test":
                    return await original(action, user)
                return None

            with (
                patch.object(bot.service, "admin"),
                patch.object(bot, "render", side_effect=render),
            ):
                await bot.render("admin:r:apply:test", actor)

    async def bot_limit(self, value):
        async with self.db.sessions.begin() as session:
            bot = RemnawaveBot(SQLRepository(session), self.settings)
            actor = await session.get(User, self.uid)
            with patch.object(bot.service, "admin"), patch.object(bot, "render"):
                await bot.input(f"admin:r:input:limit:{self.cid}", actor, str(value))

    async def test_unconfirmed_limit_update_preserves_subscription_entitlement(self):
        before = await self.local()
        for response in (
            (200, user(hwidDeviceLimit=1)),
            (200, {"response": {}}),
            TimeoutError(),
        ):
            with (
                patch(
                    "app.services.remnawave.aiohttp.ClientSession",
                    return_value=Wire(response),
                ),
                self.assertRaises(ValueError),
            ):
                await self.bot_limit(3)
        self.assertEqual(await self.local(), before)

    async def test_confirmed_limit_updates_both_local_entitlements(self):
        with patch(
            "app.services.remnawave.aiohttp.ClientSession",
            return_value=Wire((200, user(hwidDeviceLimit=3))),
        ):
            await self.bot_limit(3)
        self.assertEqual((await self.local())[3:5], (3, 3))

    async def test_timeout_then_retry_does_not_invent_deletion(self):
        before = await self.local()
        with (
            patch(
                "app.services.remnawave.aiohttp.ClientSession",
                return_value=Wire(TimeoutError()),
            ),
            self.assertRaises(ValueError),
        ):
            await self.control("deleted")
        self.assertEqual(await self.local(), before)
        wire = Wire((404, {"errorCode": "A025"}), (404, {"errorCode": "A025"}))
        with patch("app.services.remnawave.aiohttp.ClientSession", return_value=wire):
            await self.control("deleted")
        self.assertEqual((await self.local())[0], "deleted")

    async def test_negative_traffic_reset_does_not_zero_local_usage(self):
        before = await self.local()
        with (
            patch(
                "app.services.remnawave.aiohttp.ClientSession",
                return_value=Wire((200, {"response": {"isSuccess": False}})),
            ),
            self.assertRaises(ValueError),
        ):
            await self.bot_action("traffic")
        self.assertEqual(await self.local(), before)

    async def test_confirmed_traffic_reset_zeroes_local_usage(self):
        with patch(
            "app.services.remnawave.aiohttp.ClientSession",
            return_value=Wire((200, user())),
        ):
            await self.bot_action("traffic")
        self.assertEqual((await self.local())[2], 0)

    async def test_ambiguous_device_reset_preserves_devices(self):
        before = await self.local()
        with (
            patch(
                "app.services.remnawave.aiohttp.ClientSession",
                return_value=Wire((200, {"response": {}})),
            ),
            self.assertRaises(ValueError),
        ):
            await self.bot_action("devices")
        self.assertEqual(await self.local(), before)

    async def test_confirmed_device_reset_removes_only_after_confirmation(self):
        with patch(
            "app.services.remnawave.aiohttp.ClientSession",
            return_value=Wire((200, {"response": {"total": 0, "devices": []}})),
        ):
            await self.bot_action("devices")
        self.assertEqual((await self.local())[-1], ["disconnected"])


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class JournalConfirmation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_vless_renewal import VlessRenewalConsistency

        await VlessRenewalConsistency.asyncSetUp(self)
        self.adapter = api()
        provider = patch(
            "app.services.remnawave.RemnawaveService.api", return_value=self.adapter
        )
        provider.start()
        self.addCleanup(provider.stop)

    checkout = BillingConcurrency.checkout

    async def operations(self):
        from test_vless_renewal import VlessRenewalConsistency

        return await VlessRenewalConsistency.operations(self)

    async def assert_local(self, *args, **kwargs):
        from test_vless_renewal import VlessRenewalConsistency

        await VlessRenewalConsistency.assert_local(self, *args, **kwargs)

    def remote(self, expiry, status="DISABLED"):
        return user(id=101, expireAt=expiry.isoformat(), status=status)

    async def test_negative_patch_retains_journal_and_recovers_verified_paid_term(self):
        wire = Wire(
            (200, self.remote(self.initial)),
            (200, {"response": {"isSuccess": False}}),
            (200, self.remote(self.initial)),
        )
        with patch("app.services.remnawave.aiohttp.ClientSession", return_value=wire):
            result = await self.checkout(self.sid, 1, "i08-negative-patch")
        self.assertEqual(result.status_code, 409, result.text)
        await self.assert_local()
        operation = (await self.operations())[0]
        self.assertEqual(
            (operation.state, operation.patch_state),
            ("outcome_unknown", "outcome_unknown"),
        )
        # A later authoritative observation resolves the uncertain result. It
        # must not issue another PATCH, charge, or add a second paid period.
        wire = Wire(
            (200, self.remote(operation.expires_at)),
            (200, self.remote(operation.expires_at)),
            (200, self.remote(operation.expires_at, "ACTIVE")),
            (200, self.remote(operation.expires_at, "ACTIVE")),
        )
        with patch("app.services.remnawave.aiohttp.ClientSession", return_value=wire):
            first = await self.checkout(self.sid, 1, "i08-negative-patch")
            duplicate = await self.checkout(self.sid, 1, "i08-negative-patch")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), duplicate.json())
        self.assertNotIn("PATCH", [call[0] for call in wire.calls])
        await self.assert_local(operation.expires_at, 1, 1, 10_000, True)
        self.assertEqual((await self.operations())[0].state, "applied")

    async def test_ambiguous_enable_preserves_patch_progress_until_confirmed(self):
        from test_billing_concurrency import NOW

        expiry = NOW.replace(month=2, day=28)
        wire = Wire(
            (200, self.remote(self.initial)),
            (200, self.remote(expiry)),
            (200, self.remote(expiry)),
            (200, self.remote(expiry)),
            (200, {"response": {}}),
            (200, self.remote(expiry)),
        )
        with patch("app.services.remnawave.aiohttp.ClientSession", return_value=wire):
            result = await self.checkout(self.sid, 1, "i08-ambiguous-enable")
        self.assertEqual(result.status_code, 409, result.text)
        await self.assert_local()
        operation = (await self.operations())[0]
        self.assertEqual(
            (operation.state, operation.patch_state), ("patch_confirmed", "confirmed")
        )
        wire = Wire(
            (200, self.remote(expiry, "ACTIVE")), (200, self.remote(expiry, "ACTIVE"))
        )
        with patch("app.services.remnawave.aiohttp.ClientSession", return_value=wire):
            result = await self.checkout(self.sid, 1, "i08-ambiguous-enable")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(all(call[0] == "GET" for call in wire.calls))
        await self.assert_local(expiry, 1, 1, 10_000, True)
