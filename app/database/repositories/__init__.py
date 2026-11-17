from typing import Protocol
from contextlib import asynccontextmanager
from sqlalchemy import select, func, update
from app.database.models import User, Subscription, BalanceTransaction, Referral, PromoCode, PromoActivation, Tariff


class Repository(Protocol):
    async def user(self, telegram_id: int) -> User | None: ...
    async def subscriptions(self, user_id: int) -> list[Subscription]: ...
    async def balance(self, user_id: int) -> int: ...
    async def claim_trial(self, user_id: int) -> bool: ...
    async def add(self, record): ...


class SQLRepository:
    def __init__(self, session):
        self.session = session
        self._screen_cache = None
        self._screen_prefixes = set()
        self._screen_tariffs = None

    @asynccontextmanager
    async def screen_cache(self):
        """One render only: never cache account permissions or prices across requests."""
        if self._screen_cache is not None:
            yield
            return
        self._screen_cache = {}
        try:
            await self.prefetch_settings(prefixes=('button:', 'button_icon:', 'button_style:', 'button_visible:', 'button_position:', 'custom_button:', 'media_type:', 'media_file:', 'image:', 'intro:', 'legacy_tariff:'), keys=('brand','welcome','trial_enabled','trial_hours','promos_enabled'))
            yield
        finally:
            self._screen_cache = None
            self._screen_prefixes.clear()
            self._screen_tariffs = None

    async def prefetch_settings(self, keys=(), prefixes=()):
        if self._screen_cache is None:return
        from app.database.models import AppSetting
        from sqlalchemy import or_
        keys=tuple(set(keys)-self._screen_cache.keys())
        prefixes=tuple(p for p in prefixes if p not in self._screen_prefixes)
        if not keys and not prefixes:return
        conditions=([AppSetting.key.in_(keys)] if keys else [])+[AppSetting.key.startswith(p) for p in prefixes]
        values=dict((await self.session.execute(select(AppSetting.key,AppSetting.value).where(or_(*conditions)))).all())
        self._screen_cache.update({k:None for k in keys});self._screen_cache.update(values)
        self._screen_prefixes.update(prefixes)

    async def add(self, record):
        self.session.add(record)
        await self.session.flush()
        from app.database.models import AccessKey, ProxyCredential, AdminAudit
        from app.services.activity import record as history, AUDIT_NAMES
        if isinstance(record, (AccessKey, ProxyCredential)):
            from app.services.accounts import ensure_account
            account=await self.session.get(User,record.user_id)
            before=account.freeman_id
            await ensure_account(self,account)
            if not before and account.freeman_id:
                await history(self, f'user.registered:{account.id}', f'👤 Зарегистрирован клиент после получения доступа · {account.freeman_id}')
        if isinstance(record, User):
            from app.services.accounts import ensure_account
            await ensure_account(self,record)
            if record.freeman_id:await history(self, f'user.registered:{record.id}', f'👤 Зарегистрирован новый клиент\nTelegram ID: {record.telegram_id}\nИмя: {record.display_name}\nUsername: {"@"+record.username if record.username else "—"}')
        elif isinstance(record, Subscription):
            user = await self.session.get(User, record.user_id)
            await history(self, f'subscription.created:{record.id}', f'📦 Выдана подписка #{record.id}\nКлиент: {user.telegram_id}\nТип: {"Пробная" if record.kind == "trial" else "Платная"}\nДо: {record.expires_at:%d.%m.%Y %H:%M} UTC')
        elif isinstance(record, AccessKey):
            user = await self.session.get(User, record.user_id)
            await history(self, f'key.created:{record.id}', f'🔑 Создан ключ #{record.id} · {record.name}\nКлиент: {user.telegram_id}\nДо: {record.expires_at:%d.%m.%Y %H:%M} UTC')
        elif isinstance(record, AdminAudit) and record.action in AUDIT_NAMES:
            text = f'⚙️ {AUDIT_NAMES[record.action]}\nОбъект: #{record.target}\nАдминистратор: {record.actor_telegram_id}'
            if record.action in ('extend','subfreeze') and record.target.isdigit():
                sub = await self.session.get(Subscription,int(record.target))
                if sub:
                    user = await self.session.get(User,sub.user_id)
                    text += f'\nКлиент: {user.telegram_id}\nПодписка: #{sub.id}\nДо: {sub.expires_at:%d.%m.%Y %H:%M} UTC'
            await history(self, f'admin.action:{record.id}', text)
        elif isinstance(record, BalanceTransaction) and record.type in ('referral_reward','referral_reversal','withdrawal_hold','withdrawal_release'):
            from app.services.screens import money
            labels = {'referral_reward':'Реферальный бонус','referral_reversal':'Сторнирование бонуса','withdrawal_hold':'Заявка на вывод: резерв','withdrawal_release':'Возврат резерва'}
            user = await self.session.get(User,record.user_id)
            await history(self,f'balance.event:{record.id}',f'💰 {labels[record.type]}\nКлиент: {user.telegram_id}\nСумма: {money(record.amount)}\n{record.description}')
        return record

    async def user(self, telegram_id):
        return await self.session.scalar(select(User).where(User.telegram_id == telegram_id))

    async def claim_trial(self, user_id):
        result = await self.session.execute(update(User).where(User.id == user_id, User.trial_used.is_(False)).values(trial_used=True))
        return result.rowcount == 1

    async def subscriptions(self, user_id):
        return list(await self.session.scalars(select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.id.desc())))

    async def subscription(self, user_id, subscription_id):
        return await self.session.scalar(select(Subscription).where(Subscription.user_id == user_id, Subscription.id == subscription_id))

    async def balance(self, user_id):
        return await self.session.scalar(select(func.coalesce(func.sum(BalanceTransaction.amount), 0)).where(BalanceTransaction.user_id == user_id))

    async def transactions(self, user_id):
        return list(await self.session.scalars(select(BalanceTransaction).where(BalanceTransaction.user_id == user_id).order_by(BalanceTransaction.id.desc()).limit(20)))

    async def referrals(self, user_id):
        return list(await self.session.scalars(select(Referral).where(Referral.inviter_id == user_id).order_by(Referral.id.desc()).limit(20)))

    async def referral_count(self, user_id):
        return await self.session.scalar(select(func.count()).select_from(Referral).where(Referral.inviter_id == user_id))

    async def promo(self, code):
        return await self.session.scalar(select(PromoCode).where(PromoCode.code == code))

    async def activation(self, user_id, promo_id):
        return await self.session.scalar(select(PromoActivation).where(PromoActivation.user_id == user_id, PromoActivation.promo_id == promo_id))

    async def tariffs(self):
        if self._screen_tariffs is not None:return self._screen_tariffs
        rows=list(await self.session.scalars(select(Tariff).order_by(Tariff.months, Tariff.device_limit, Tariff.id)))
        if self._screen_cache is not None:self._screen_tariffs=rows
        return rows

    async def tariff(self, months):
        # New bot callbacks use id<N>. Numeric selectors are legacy durations;
        # their binding is frozen by the explicit tariff migration.
        if isinstance(months, str) and months.startswith("id"):
            return await self.tariff_by_id(int(months[2:]))
        if months is None:
            return None
        months = int(months)
        legacy = await self.setting(f"legacy_tariff:{months}")
        if legacy:
            tariff = await self.tariff_by_id(int(legacy))
            return tariff if tariff and tariff.months == months else None
        if self._screen_tariffs is not None:
            matches=[t for t in self._screen_tariffs if t.months==months]
            return matches[0] if len(matches)==1 else None
        rows = list(
            await self.session.scalars(
                select(Tariff).where(Tariff.months == months).limit(2)
            )
        )
        return rows[0] if len(rows) == 1 else None

    async def select_tariff(self, months=None, tariff_id=None):
        tariff = (
            await self.tariff_by_id(tariff_id)
            if tariff_id is not None
            else await self.tariff(months)
        )
        return (
            tariff if tariff and (months is None or tariff.months == months) else None
        )

    async def tariff_setting(self, tariff, name, default=""):
        value = await self.setting(f"{name}:id{tariff.id}", None)
        if value is not None:
            return value
        legacy = await self.setting(f"legacy_tariff:{tariff.months}")
        if not legacy:
            original = await self.tariff(tariff.months)
            legacy = str(original.id) if original else ""
        if legacy == str(tariff.id):
            return await self.setting(f"{name}:{tariff.months}", default)
        return default

    async def set_tariff_setting(self, tariff, name, value):
        await self.set_setting(f"{name}:id{tariff.id}", value)

    async def order_tariff(self, order):
        tariff = (
            await self.tariff_by_id(order["tariff_id"])
            if order.get("tariff_id") is not None
            else await self.tariff(order.get("months"))
        )
        if not tariff:
            raise ValueError(
                "Тариф заказа не найден. Обратитесь в поддержку; повторно платить не нужно."
            )
        # Paid orders own their saved period, even if the catalogue changes.
        from types import SimpleNamespace

        return SimpleNamespace(
            id=tariff.id,
            months=order["months"],
            price=tariff.price,
            device_limit=order.get("devices", tariff.device_limit),
        )

    async def auto_tariff(self, sid):
        tid = await self.setting(f"auto_renew_tariff:{sid}")
        return (
            await self.tariff_by_id(int(tid))
            if tid
            else await self.tariff(
                int(await self.setting(f"auto_renew_months:{sid}", "1"))
            )
        )

    async def tariff_by_id(self, tariff_id):
        if self._screen_tariffs is not None:return next((t for t in self._screen_tariffs if t.id==int(tariff_id)),None)
        return await self.session.get(Tariff, tariff_id)

    async def lock_tariff(self, tariff):
        """Fence removal until a short purchase-intent transaction commits.

        Call when staging durable orders, not during provider HTTP/recovery.
        Physical deletion takes FOR UPDATE; concurrent intent readers share
        KEY SHARE and never serialize independent customer purchases.
        """
        return await self.lock_catalog_item(Tariff, tariff, 'vpn')

    async def lock_catalog_item(self, model, item, kind):
        if item is None:
            return None
        if self.session.bind.dialect.name == 'postgresql':
            from hashlib import sha256
            from sqlalchemy import text
            key = int.from_bytes(sha256(f'catalog:{kind}:{item.id}'.encode()).digest()[:8], 'big', signed=True)
            # Never wait behind a catalogue writer while holding a customer's
            # lock: other staging paths acquire their customer lock later.
            if not await self.session.scalar(text('SELECT pg_try_advisory_xact_lock_shared(:key)'), {'key': key}):
                return None
        return await self.session.scalar(
            select(model).where(model.id == item.id)
            .with_for_update(read=True, key_share=True)
            .execution_options(populate_existing=True)
        )

    async def setting(self, key, default=''):
        from app.database.models import AppSetting
        cache=self._screen_cache
        if cache is not None:
            if key not in cache and any(key.startswith(p) for p in self._screen_prefixes):cache[key]=None
            if key in cache:return default if cache[key] is None else cache[key]
        row = await self.session.get(AppSetting, key)
        if cache is not None:cache[key]=row.value if row else None
        return row.value if row else default

    async def keys(self, user_id, offset=0):
        from app.database.models import AccessKey
        return list(await self.session.scalars(select(AccessKey).where(AccessKey.user_id == user_id, AccessKey.status != 'deleted').order_by(AccessKey.id.desc()).offset(offset).limit(9)))

    async def set_setting(self, key, value):
        from app.database.models import AppSetting
        if self.session.bind.dialect.name=='postgresql':
            from sqlalchemy.dialects.postgresql import insert
            statement=insert(AppSetting).values(key=key,value=str(value))
            statement=statement.on_conflict_do_update(index_elements=[AppSetting.key],set_={'value':statement.excluded.value}).returning(AppSetting)
            await self.session.scalar(statement,execution_options={'populate_existing':True})
            if self._screen_cache is not None:self._screen_cache[key]=str(value)
            return
        row = await self.session.get(AppSetting, key)
        if row:
            row.value = str(value)
        else:
            await self.add(AppSetting(key=key, value=str(value)))
        if self._screen_cache is not None:self._screen_cache[key]=str(value)

    async def settings_prefix(self, prefix):
        from app.database.models import AppSetting
        if self._screen_cache is not None:
            await self.prefetch_settings(prefixes=(prefix,))
            return {k:v for k,v in self._screen_cache.items() if k.startswith(prefix) and v is not None}
        rows = await self.session.scalars(select(AppSetting).where(AppSetting.key.startswith(prefix)))
        return {row.key: row.value for row in rows}

    async def track_funnel(self, user_id, stage, context=''):
        from app.database.models import FunnelEvent
        exists = await self.session.scalar(select(FunnelEvent.id).where(FunnelEvent.user_id==user_id,FunnelEvent.stage==stage))
        if not exists:
            await self.add(FunnelEvent(user_id=user_id,stage=stage,context=context[:100]))
