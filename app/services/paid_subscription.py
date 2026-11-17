"""The same connection receipt for card/balance and Telegram Stars payments."""
from app.services.screens import Screen


async def success_screen(repo, settings, payment, user):
    sid = payment.subscription_id
    if not sid:
        return Screen('✅ Оплата подтверждена. Подписка готовится.', [[('📦 Мои подписки', 'subscriptions')]])
    from app.services.vless_provision import connection
    subscription = await repo.subscription(user.id, sid)
    receipt = await connection(repo, settings, subscription, user, paid=True) if subscription else None
    if receipt:
        return receipt
    kid = await repo.setting(f'subscription_key:{sid}')
    if kid and await repo.setting(f'demo_file:{kid}'):
        from app.services.demo_keys import key_screen
        return await key_screen(repo, settings, user, int(kid), sid, 'qr')
    from app.services.vpn import VpnService
    return await VpnService(repo, settings).render(f'v:locations:{sid}', user)
