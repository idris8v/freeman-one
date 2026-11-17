"""Assemble website routes. Domain handlers live in app.web.cabinet.

Keep website_router and STATIC as the public entry points used by main and tests.
"""

from fastapi import APIRouter

from app.web.cabinet.auth import auth_router
from app.web.cabinet.content import content_router
from app.web.cabinet.context import STATIC as STATIC
from app.web.cabinet.context import WebContext
from app.web.cabinet.keys import keys_router
from app.web.cabinet.payments import payments_router
from app.web.cabinet.profile import profile_router
from app.web.cabinet.proxy import proxy_router
from app.web.cabinet.referrals import referrals_router
from app.web.cabinet.support import support_router
from app.web.cabinet.support_media import support_media_router
from app.web.cabinet.support_settings import support_settings_router


def website_router(database, settings):
    context = WebContext(database, settings)
    current_user, session_token = context.current_user, context.session_token
    from app.web.cabinet.context import COOKIE

    router = APIRouter()
    from app.web.request_performance import performance_router
    router.include_router(performance_router(context))
    from app.web.key_antig import key_antig_router
    from app.web.manual_price import manual_price_router
    router.include_router(key_antig_router(database,settings,current_user))
    router.include_router(manual_price_router(database,settings,current_user))
    from app.web.subscription_transfer import transfer_router
    router.include_router(transfer_router(database,settings,current_user))
    from app.web.support_agent import support_agent_router
    router.include_router(support_agent_router(database,settings,current_user))
    from app.web.subscriptions import subscriptions_router
    router.include_router(subscriptions_router(context))
    from app.web.freeman_ai import ai_router

    router.include_router(ai_router(database, settings, current_user))
    from app.web.support_agent_actions import actions_router
    router.include_router(actions_router(database,settings,current_user))

    from app.web.account_management import management_router

    router.include_router(
        management_router(database, settings, current_user, session_token, COOKIE)
    )

    from app.web.customer_identity import identity_router

    router.include_router(identity_router(database, settings, current_user))

    from app.web.accounts import account_router

    router.include_router(
        account_router(database, settings, current_user, session_token, COOKIE)
    )

    from app.web.guest_purchase import guest_router

    router.include_router(
        guest_router(database, settings, session_token, COOKIE, current_user)
    )

    from app.web.admin_dashboard import admin_dashboard_router

    router.include_router(admin_dashboard_router(database, settings, current_user))

    from app.web.catalog import catalog_router

    router.include_router(catalog_router(database, settings, current_user))

    from app.web.customer_profile import customer_profile_router

    router.include_router(customer_profile_router(database, settings, current_user))

    from app.web.offers import offers_router

    router.include_router(offers_router(database, settings, current_user))

    from app.web.admin_reports import admin_reports_router

    router.include_router(admin_reports_router(database, settings, current_user))

    from app.web.admin_payments import admin_payments_router

    router.include_router(admin_payments_router(database, settings, current_user))

    from app.web.admin_proxy import admin_proxy_router

    router.include_router(admin_proxy_router(database, settings, current_user))

    from app.web.admin_referrals import admin_referrals_router
    router.include_router(admin_referrals_router(database,settings,current_user))

    from app.web.proxy_control import proxy_control_router

    router.include_router(proxy_control_router(database, settings, current_user))

    from app.web.posts import posts_router

    router.include_router(posts_router(database, settings, current_user))

    # Direct composition preserves API endpoint introspection used by AI tools.
    for factory in (
        auth_router,
        profile_router,
        payments_router,
        referrals_router,
        proxy_router,
        keys_router,
        support_router,
        support_media_router,
        support_settings_router,
        content_router,
    ):
        router.routes.extend(factory(context).routes)
    return router
