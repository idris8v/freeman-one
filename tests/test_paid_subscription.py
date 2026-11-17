import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.paid_subscription import success_screen
from app.utils.formatting import traffic


class PaidSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_subscription_has_pending_receipt(self):
        result = await success_screen(None, None, SimpleNamespace(subscription_id=None), None)
        self.assertIn('готовится', result.text)

    async def test_existing_connection_is_used_for_each_payment_method(self):
        from app.services.stars import StarService
        from app.services.topups import SubscriptionCheckoutService
        repo = SimpleNamespace(subscription=AsyncMock(return_value=SimpleNamespace(id=3)))
        user = SimpleNamespace(id=1)
        with patch('app.services.vless_provision.connection', new=AsyncMock(return_value='verified receipt')):
            for cls in (StarService, SubscriptionCheckoutService):
                service = object.__new__(cls)
                service.repo, service.settings = repo, SimpleNamespace()
                result = await service.success_screen(SimpleNamespace(subscription_id=3), user)
                self.assertEqual(result, 'verified receipt')

    def test_traffic_units_keep_existing_display_contract(self):
        for value, expected in [(None, '0 Б'), (1023, '1023 Б'), (1024, '1.0 КБ'), (1024**3, '1.0 ГБ')]:
            self.assertEqual(traffic(value), expected)
