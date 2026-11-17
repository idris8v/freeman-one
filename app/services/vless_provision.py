from app.services.subscription_labels import expiry_date
"""Shared VLESS issuance and access control for website and Telegram bot."""

from app.database.models import AccessKey, VlessCredential, AdminAudit, now
from app.services.remnawave import RemnawaveService, RemnawaveApiError


async def provision(
    repo, settings, sub, user, server, actor, *, operation_key=None, automatic=False
):
    from app.services.vless_creation import validate_limits

    validate_limits(sub.device_limit, 0)
    from datetime import datetime
    from sqlalchemy import select
    from app.database.models import Subscription, VlessCreation
    from app.services import vless_creation as journal

    existing = await repo.session.scalar(
        select(VlessCredential).where(VlessCredential.subscription_id == sub.id)
    )
    if existing:
        if existing.status == "deleted":
            raise ValueError("Ключ удалён; создайте новую подписку")
        return existing
    identity = await journal.operation_id(repo, settings, sub, user, operation_key)
    from app.services.vless_fallback import NotCreated, PlacementChanged, move
    from app.database.models import RemnawaveServer

    traffic_limit = int(await repo.setting(f"subscription_traffic_limit:{sub.id}", "0"))
    attempted = set()
    while True:
        operation = await journal.recorded(repo, identity)
        if operation and (
            operation.server_id != server.id or operation.server_url != server.api_url
        ):
            if not automatic:
                raise ValueError("Заказ уже закреплён за другой панелью VLESS.")
            server = await repo.session.get(
                RemnawaveServer, operation.server_id, populate_existing=True
            )
            if not server or server.api_url != operation.server_url:
                raise ValueError("Панель исходной выдачи VLESS недоступна.")
        if not server.internal_squad_uuid:
            raise ValueError("В панели VLESS нужно указать Internal Squad")
        try:
            # A previously proven failure can resume placement after a restart.
            if automatic and operation and operation.state == "failed":
                alternative = await move(repo, identity, server.id, excluded=attempted)
                if alternative:
                    server = alternative
                elif server.id in attempted:
                    raise NotCreated("Нет безопасной панели для выдачи VLESS.")
            attempted.add(server.id)
            body, remote = await journal.confirm_remote(
                repo,
                settings,
                identity,
                sub,
                user,
                server,
                traffic_limit,
                automatic=automatic,
            )
            break
        except PlacementChanged:
            if not automatic:
                raise
            # A concurrent caller may have safely advanced placement. Reload the
            # durable binding; never continue with the stale caller's panel.
            current = await journal.recorded(repo, identity)
            if not current or current.server_id == server.id:
                raise
        except NotCreated:
            if not automatic:
                raise
            try:
                alternative = await move(repo, identity, server.id, excluded=attempted)
            except PlacementChanged:
                current = await journal.recorded(repo, identity)
                if not current or current.server_id == server.id:
                    raise
                continue
            if not alternative:
                # Another executor can reclaim the released operation before
                # this caller moves it. Follow its durable state; a confirmed
                # failure alone must not cancel that in-flight recovery.
                current = await journal.recorded(repo, identity)
                if current and (
                    current.server_id != server.id or current.state != "failed"
                ):
                    continue
                raise
            server = alternative

    # Keep this lock until the caller commits: the local association and journal
    # binding must succeed/roll back together, including concurrent requests.
    operation = await repo.session.scalar(
        select(VlessCreation)
        .where(VlessCreation.id == identity)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if operation.server_id != server.id or operation.server_url != server.api_url:
        raise ValueError("Панель выдачи изменилась до сохранения ключа.")
    if operation.credential_id:
        existing = await repo.session.get(VlessCredential, operation.credential_id)
        if (
            not existing
            or existing.subscription_id != sub.id
            or existing.user_id != user.id
            or existing.status == "deleted"
        ):
            raise ValueError(
                "Эта операция выдачи уже обработана. Проверьте исходную подписку."
            )
        return existing
    async with journal.durable_session(repo) as session:
        committed = await session.get(Subscription, sub.id)
    expiry = datetime.fromisoformat(body["expireAt"])
    if committed and abs((journal.utc(sub.expires_at) - expiry).total_seconds()) >= 1:
        raise ValueError(
            "Срок подписки изменился во время выдачи. Нужна проверка заказа."
        )
    # A rolled-back purchase can allocate another subscription ID. Its stable
    # operation retains the original promised expiry rather than adding time.
    sub.expires_at = expiry
    previous = await repo.setting(f"subscription_key:{sub.id}")
    key = await repo.session.get(AccessKey, int(previous)) if previous else None
    if key and key.user_id == user.id and key.mode == "mock":
        key.mode = "vless"
        key.expires_at = expiry
        key.status = "active"
        await repo.set_setting(f"demo_file:{key.id}", "")
    else:
        key = await repo.add(
            AccessKey(user_id=user.id, name="Подписка", mode="vless", expires_at=expiry)
        )
    await repo.set_setting(f"subscription_key:{sub.id}", str(key.id))
    credential = await repo.add(
        VlessCredential(
            server_id=server.id,
            user_id=user.id,
            subscription_id=sub.id,
            access_key_id=key.id,
            remote_user_uuid=remote["identity"],
            short_uuid=remote["short_uuid"],
            subscription_url=remote["subscription_url"],
            expires_at=expiry,
            device_limit=sub.device_limit,
            traffic_limit=traffic_limit,
            last_sync=now(),
        )
    )
    operation.subscription_id = sub.id
    operation.user_id = user.id
    operation.credential_id = credential.id
    operation.state = "associated"
    operation.updated_at = now()
    await repo.add(
        AdminAudit(
            actor_telegram_id=actor.telegram_id or 0,
            action="web_vless_provision",
            target=str(key.id),
            details=f"panel={server.id}; devices={sub.device_limit}",
        )
    )
    return credential

async def control(repo,settings,credential,action,expiry=None,*,renewal=None):
    if action=='extend' and renewal is not None:
        from app.services.vless_renewal import renew
        return await renew(repo,settings,credential,expiry,**renewal)
    if action=='extend':
        from app.services.vless_renewal import guard_expiry_sync
        await guard_expiry_sync(repo,credential,expiry)
    from app.database.models import RemnawaveServer
    panel=await repo.session.get(RemnawaveServer,credential.server_id)
    api=RemnawaveService(repo,settings).api(panel);remote=credential.remote_user_uuid
    try:
        if action=='extend':await api.update_user({('id' if remote.isdigit() else 'uuid'):int(remote) if remote.isdigit() else remote,'expireAt':expiry.isoformat(),'hwidDeviceLimit':credential.device_limit})
        elif action=='block':await api.disable_user(remote)
        elif action=='unblock':await api.enable_user(remote)
        elif action=='delete':
            try:await api.delete_user(remote)
            except RemnawaveApiError as error:
                if error.status!=404 or error.error_code not in {"A025","A063"}:raise
                # A missing remote user is already revoked. Verify before completing locally.
                try:await api.user(remote)
                except RemnawaveApiError as check:
                    if check.status!=404 or check.error_code not in {"A025","A063"}:raise
                else:raise error
        elif action=='rotate':
            result=await api.revoke_user(remote)
            data=result.get('response',result)
            if not data.get('subscriptionUrl') or data['subscriptionUrl']==credential.subscription_url:raise ValueError('Панель не подтвердила новую ссылку подписки')
            credential.subscription_url=data['subscriptionUrl'];credential.short_uuid=data.get('shortUuid') or ''
        else:raise ValueError('Действие VLESS недоступно')
        credential.last_sync=now()
    except RemnawaveApiError as e:raise ValueError('Панель VLESS не подтвердила изменение ключа') from e

async def ensure(repo, settings, sub, user, *, operation_key=None):
    from sqlalchemy import select
    from app.database.models import RemnawaveServer
    from app.services import vless_creation as journal

    journal.validate_limits(sub.device_limit, 0)

    existing = await repo.session.scalar(
        select(VlessCredential).where(VlessCredential.subscription_id == sub.id)
    )
    if existing:
        return existing
    panels = list(
        await repo.session.scalars(
            select(RemnawaveServer).order_by(
                RemnawaveServer.priority, RemnawaveServer.id
            )
        )
    )
    if not panels:
        from app.services.demo_keys import demo_enabled

        if demo_enabled(repo):
            return None
        raise ValueError(
            "Панель VLESS не настроена. Подключение не выдано. "
            "Для оплаченного заказа повторно платить не нужно."
        )
    from app.services.bot_antig_test import profile
    access = await profile(repo,sub.id)
    if access:
        panel=next((p for p in panels if p.id==access['panel']),None)
        if not panel or panel.status!='active' or panel.health=='offline':
            raise ValueError('Сервер выдачи подписки недоступен. Повторите позже.')
        return await provision(repo,settings,sub,user,panel,user,operation_key=operation_key,automatic=False)
    identity = await journal.operation_id(repo, settings, sub, user, operation_key)
    operation = await journal.recorded(repo, identity)
    if operation:
        from app.services.vless_fallback import PlacementChanged, move

        panel = None
        if operation.state == "failed":
            try:
                panel = await move(repo, identity, operation.server_id)
            except PlacementChanged:
                # Another executor safely moved this same operation after our
                # journal read. Follow its durable binding; never choose anew.
                operation = await journal.recorded(repo, identity)
                if operation is None:
                    raise ValueError("Не удалось восстановить операцию выдачи VLESS.")
        if panel is None:
            panel = next((p for p in panels if p.id == operation.server_id), None)
        if not panel:
            raise ValueError(
                "Панель исходной выдачи VLESS недоступна. Нужна проверка заказа."
            )
        return await provision(
            repo,
            settings,
            sub,
            user,
            panel,
            user,
            operation_key=operation_key,
            automatic=True,
        )
    from app.services.vless_capacity import CapacityUnavailable

    for panel in panels:
        if (
            panel.status == "active"
            and panel.health != "offline"
            and panel.internal_squad_uuid
            and panel.users_total < panel.max_users
        ):
            try:
                return await provision(
                    repo,
                    settings,
                    sub,
                    user,
                    panel,
                    user,
                    operation_key=operation_key,
                    automatic=True,
                )
            except CapacityUnavailable:
                continue
    raise ValueError(
        "Выдача подписки временно недоступна. Обратитесь в поддержку."
    )

async def connection(repo, settings, sub, user, paid=False, *, restored=False):
    from sqlalchemy import select
    from app.services.screens import Screen, time_remaining
    from html import escape
    from app.services.qr_image import png
    c=await repo.session.scalar(select(VlessCredential).where(VlessCredential.subscription_id==sub.id,VlessCredential.user_id==user.id))
    if not c:return None
    expiry=sub.expires_at.replace(tzinfo=__import__('datetime').timezone.utc)
    expired=expiry<=now()
    subscription_status=await repo.setting(f'subscription_status:{sub.id}','active')
    key=await repo.session.get(AccessKey,c.access_key_id)
    active=c.status=='active' and subscription_status=='active' and key and key.status=='active' and not expired
    if c.status=='deleted' or await repo.setting(f'subscription_removed:{sub.id}')=='1':raise ValueError('Подписка удалена.')
    if paid and not active:raise ValueError('Подключение недоступно: ключ заблокирован или срок истёк.')
    image=await png(c.subscription_url)
    from app.services.bot_catalog import devices_label
    from app.services.subscription_labels import current,automatic
    name,term=await current(repo,sub,key)
    summary = name if automatic(name) else ' · '.join(filter(None, (name, devices_label(sub.device_limit), term)))
    heading = '✅ <b>Платёж принят</b>' if paid else '🔑 <b>Ваша подписка</b>'
    validity = f'📅 <b>Действует до:</b> {expiry_date(sub.expires_at)}'
    if active and not paid:
        validity += f'\n<b>Осталось:</b> {time_remaining(sub.expires_at)}'
    if not active:
        validity = ('🔴 Срок подписки истёк' if expired else '🔴 Доступ приостановлен')+'\n'+validity
    hint = ('📲 Нажмите «Подключить» и следуйте подсказкам на странице. '
            'Можно также скопировать ключ и добавить его в приложение.')
    if not active:
        hint = ('🔄 Продлите подписку, чтобы снова подключиться.' if expired
                else '💬 Поддержка подскажет, как восстановить доступ.')
    text = (f'{heading}\n\n<b>{escape(summary)}</b>\n{validity}\n\n'
            f'🔗 <b>Ссылка подключения</b>\n<code>{escape(c.subscription_url)}</code>\n\n{hint}')
    from app.services.bot_antig_addon import has_access
    if active and await has_access(repo,sub):
        text+='\n\n🛡 <b>Антиглушилка подключена</b>\nДополнительный сервер уже в этой подписке.'
    connect='url:'+c.subscription_url
    copy='copy:'+c.subscription_url
    rows=([[('📲 Подключить',connect)]] if active else []) + [[('📋 Скопировать ключ',copy)]]
    gift_target=None
    from app.services.public_proxy import connection_url
    if active and not await connection_url(repo):
        from app.services.proxy_control import config,subscription_eligible,sync_included,gift_visible
        from app.services.proxies import ProxyService,TelemtError
        cfg=await config(repo)
        visible=await gift_visible(repo,sub)
        if cfg['auto_included'] and (paid or visible) and await subscription_eligible(repo,sub):
            if paid and restored:
                proxy=None
            elif paid:
                try:proxy=await sync_included(repo,settings,sub,paid)
                except (ValueError,TelemtError):proxy=None
            else:
                # Display existing entitlement; maintenance/payment flows reconcile it.
                from app.database.models import ProxyCredential,ProxyAccessDetails
                proxy=await repo.session.scalar(select(ProxyCredential).join(ProxyAccessDetails,ProxyAccessDetails.credential_id==ProxyCredential.id).where(ProxyCredential.user_id==sub.user_id,ProxyAccessDetails.source=='included',ProxyCredential.status=='active',ProxyCredential.expires_at>now()).order_by(ProxyCredential.id).limit(1))
            if await gift_visible(repo,sub,paid and not restored):
                gift_target='url:'+ProxyService(repo,settings).decrypt(proxy.link) if proxy and proxy.link and proxy.status=='active' else 'p:home'
                if not paid:
                    text+='\n\n<blockquote>🎁 <b>Также вам подарок!</b>\nВсем нашим клиентам с подходящей VPN-подпиской мы бесплатно выдаём прокси для Telegram. Подключите его — и ваш Telegram будет работать без VPN.\n\nОдин подарочный прокси на аккаунт, до двух активных подключений. Действует, пока есть активная подходящая VPN-подписка. Не передавайте ссылку другим людям.'
                    if gift_target=='p:home':text+='\nПодключение подготавливается. Проверьте его в разделе Telegram Proxy.'
    if paid:
        from app.services.paid_install_support import help_target
        from app.services.paid_navigation import route
        if gift_target:
            text+='\n\n🎁 Подарочный Telegram Proxy — в разделе «Дополнительно».'
        rows += [[('✏️ Назвать ключ',route(f'rename:{sub.id}',sub.id))],
                 [('💬 Помощь с установкой',help_target(settings,sub.id))],
                 [('⚙️ Дополнительно',f'subscription_extra:{sub.id}:paid')],
                 [('← Назад','home')]]
        return Screen(text, rows, appearance_page='receipt', photo=(image,f'vless-{c.id}.png'),
                      button_options={connect:{'style':'primary'}}, structured=True)
    if gift_target:text+='</blockquote>'
    from app.services.trial_offer import CONVERSION_LABEL
    enabled=await repo.setting(f'auto_renew:{sub.id}','0')=='1'
    renewal_label=CONVERSION_LABEL if sub.kind=='trial' else '🔄 Продлить'
    rows += [[(renewal_label,f'renew:{sub.id}')],[('📱 Подключённые устройства',f'devices:{sub.id}')]]
    if sub.kind!='trial':
        rows += [[('🔁 Автопродление · '+('Вкл.' if enabled else 'Выкл.'),f'autorenew:{sub.id}')]]
    rows += [[('✏️ Назвать ключ',f'rename:{sub.id}')]]
    if active and sub.kind=='paid' and sub.tariff_id:rows += [[('✨ Улучшить тариф',f'up:menu:{sub.id}')]]
    if active:
        from app.services.bot_antig_price import enabled as antig_enabled
        if settings.is_owner(user.telegram_id) or await antig_enabled(repo):
            rows += [[('🛡 Антиглушилка',f'ag:menu:{sub.id}')]]
            if settings.is_owner(user.telegram_id) and not sub.tariff_id:rows += [[('🛡 Назначить цену дополнения',f'ag:price:{sub.id}')]]
    if gift_target:rows += [[('🌐 Подключить прокси',gift_target)]]
    rows += [[('⚙️ Дополнительно',f'subscription_extra:{sub.id}')]]
    rows += [[('← Назад','subscriptions')]]
    return Screen(text,rows,gift_target=gift_target,appearance_page='key_card',photo=(image,f'vless-{c.id}.png'),button_options={connect:{'style':'primary'},f'renew:{sub.id}':{'style':'success'},**({gift_target:{'style':'primary'}} if gift_target else {})},structured=True)
