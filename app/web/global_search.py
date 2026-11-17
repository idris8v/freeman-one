"""Bounded administrator search; local records only, no provider requests."""

import re
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from sqlalchemy import String, cast, or_, select, text

from app.database.models import (
    AccessKey,
    AppSetting,
    GuestOrder,
    ProxyCredential,
    User,
    VlessCredential,
    Subscription,
    VpnCredential,
)
from app.database.repositories import SQLRepository

KINDS = {"all", "client", "key", "proxy", "payment"}
LIMIT = 25


def literal(value):
    return (
        "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    )


def literal_name(sid):
    from sqlalchemy import literal as sql_literal
    return sql_literal('subscription_name:') + cast(sid,String)

async def search(session, settings, query, kind="all"):
    if not isinstance(query, str) or len(query) > 2048 or "\x00" in query:
        raise ValueError("Запрос должен содержать не более 2048 символов.")
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("Неизвестный тип поиска.")
    q = query.strip().lstrip("@")
    prefix = re.fullmatch(r"([PBSOG])[-# ]?(\d+)", q, re.I)
    number = prefix.group(2) if prefix else q.lstrip("#")
    n = (
        int(number)
        if number.isascii() and number.isdecimal() and len(number) <= 18
        else None
    )
    if len(q) < 2 and n is None:
        return {"items": [], "truncated": False, "limit_per_type": LIMIT}
    effective = kind
    if prefix:
        inferred = "key" if prefix[1].upper() == "S" else "payment"
        if kind not in ("all", inferred):
            return {"items": [], "truncated": False, "limit_per_type": LIMIT}
        effective = inferred
    values = [q]
    alias = re.fullmatch(r"(?:FO-|FRM-)(\d{1,12})", q, re.I)
    if alias:
        values += ["FRM-" + alias[1].zfill(6), "FO-" + str(int(alias[1]))]
    # A copied URL may have trailing slash/query parameters. Match its actual
    # subscription token, never merely the common hostname of every customer.
    try:
        url = urlsplit(q if "://" in q else "https://" + q)
        if url.hostname and "." in url.hostname and url.path.strip("/"):
            token = unquote(url.path.rstrip("/").rsplit("/", 1)[-1])
            if len(token) >= 4 and token not in values:
                values.append(token)
    except ValueError:
        pass

    def matches(column):
        return or_(*(column.ilike(literal(v), escape="\\") for v in values))

    criteria = [
        matches(c)
        for c in (
            User.first_name,
            User.username,
            User.account_username,
            User.email,
            User.freeman_id,
        )
    ]
    if n is not None and not prefix:
        criteria += [User.id == n, User.telegram_id == n]
    users = list(
        await session.scalars(
            select(User).where(or_(*criteria)).order_by(User.id.desc()).limit(LIMIT + 1)
        )
    )
    from app.services.external_contacts import find

    contact_id = await find(SQLRepository(session), settings, q)
    if contact_id and all(u.id != contact_id for u in users):
        user = await session.get(User, contact_id)
        if user:
            users.append(user)
    user_ids = [u.id for u in users]
    groups = []
    if effective in ("all", "client"):
        groups.append(("client", users))
    if effective in ("all", "key"):
        sub_keys = select(AppSetting.value).where(
            AppSetting.key == f"subscription_key:{n}"
        )
        remote = select(VlessCredential.access_key_id).where(
            or_(
                matches(VlessCredential.remote_user_uuid),
                matches(VlessCredential.short_uuid),
                matches(VlessCredential.subscription_url),
                VlessCredential.subscription_id == n if n is not None else False,
            )
        )
        conditions = [
            matches(AccessKey.name),
            AccessKey.id.in_(select(VlessCredential.access_key_id).join(AppSetting,AppSetting.key==literal_name(VlessCredential.subscription_id)).where(matches(AppSetting.value))),
            AccessKey.id.in_(select(VpnCredential.access_key_id).join(AppSetting,AppSetting.key==literal_name(VpnCredential.subscription_id)).where(matches(AppSetting.value))),
            AccessKey.user_id.in_(user_ids),
            AccessKey.id.in_(remote),
            AccessKey.id.in_(
                select(VpnCredential.access_key_id).where(
                    matches(VpnCredential.remote_client_id)
                )
            ),
        ]
        if n is not None:
            conditions += [cast(AccessKey.id, String).in_(sub_keys)]
            if not prefix or prefix[1].upper() != "S":
                conditions += [AccessKey.id == n]
        rows = list(
            await session.scalars(
                select(AccessKey)
                .where(AccessKey.status != "deleted", or_(*conditions))
                .order_by(AccessKey.id.desc())
                .limit(LIMIT + 1)
            )
        )
        groups.append(("key", rows))
    if effective in ("all", "proxy"):
        rows = list(
            await session.scalars(
                select(ProxyCredential)
                .where(
                    or_(
                        matches(ProxyCredential.remote_username),
                        matches(ProxyCredential.link),
                        ProxyCredential.id == n if n is not None else False,
                        ProxyCredential.user_id.in_(user_ids),
                    )
                )
                .order_by(ProxyCredential.id.desc())
                .limit(LIMIT + 1)
            )
        )
        groups.append(("proxy", rows))
    if effective in ("all", "payment"):
        tag = prefix[1].upper() if prefix else None
        # Reuse the exact finance visibility rules used by payment details.
        # Technical balancing entries, test/gift/deleted sales must not create
        # search buttons leading to a detail endpoint that intentionally hides them.
        from app.web.admin_payments import LEDGER

        ledger = LEDGER.split(", filtered AS")[0]
        for code, record_kind in (("P", "payment"), ("B", "balance")):
            if tag not in (None, code):
                continue
            params = dict(
                record_kind=record_kind,
                number=n,
                user_ids=user_ids,
                subscription_id=n if not prefix else None,
                limit=LIMIT + 1,
            )
            patterns = []
            for index, value in enumerate(values):
                name = f"pattern{index}"
                params[name] = literal(value)
                patterns.append(
                    "concat_ws(' ',provider_id,description,order_id) ILIKE :"
                    + name
                    + " ESCAPE '\\'"
                )
            sql = (
                ledger
                + " SELECT * FROM ledger WHERE record_kind=:record_kind AND (number=:number OR subscription_id=:subscription_id OR user_id=ANY(CAST(:user_ids AS int[])) OR "
                + " OR ".join(patterns)
                + ") ORDER BY number DESC LIMIT :limit"
            )
            found = (await session.execute(text(sql), params)).mappings()
            rows = [
                SimpleNamespace(
                    id=r["number"],
                    user_id=r["user_id"],
                    status=r["raw_status"],
                    type=r["operation"],
                )
                for r in found
            ]
            groups.append((record_kind, rows))
        if tag in (None, "O", "G"):
            rows = list(
                await session.scalars(
                    select(GuestOrder)
                    .where(
                        or_(
                            matches(GuestOrder.verified_email),
                            GuestOrder.id == n if n is not None else False,
                            GuestOrder.user_id.in_(user_ids),
                        )
                    )
                    .order_by(GuestOrder.id.desc())
                    .limit(LIMIT + 1)
                )
            )
            groups.append(("order", rows))
    order_payments = {
        row.payment_id
        for kind, rows in groups
        if kind == "order"
        for row in rows[:LIMIT]
        if row.payment_id
    }
    visible_order_payments = set()
    if order_payments:
        from app.web.admin_payments import LEDGER

        visible_order_payments = set(
            await session.scalars(
                text(
                    LEDGER.split(", filtered AS")[0]
                    + " SELECT number FROM ledger WHERE record_kind='payment' AND number=ANY(CAST(:ids AS int[]))"
                ),
                {"ids": list(order_payments)},
            )
        )
    owner_ids = {
        r.id if k == "client" else r.user_id for k, rows in groups for r in rows[:LIMIT]
    } - {None}
    owners = {
        u.id: u
        for u in await session.scalars(select(User).where(User.id.in_(owner_ids)))
    }
    key_ids=[r.id for k,rows in groups if k=='key' for r in rows[:LIMIT]]
    named={}
    for credential in (VlessCredential,VpnCredential):
        for kid,name in await session.execute(select(credential.access_key_id,AppSetting.value).join(AppSetting,AppSetting.key==literal_name(credential.subscription_id)).where(credential.access_key_id.in_(key_ids))):named[kid]=name
    items = []
    for record_kind, rows in groups:
        for row in rows[:LIMIT]:
            uid = row.id if record_kind == "client" else row.user_id
            user = owners.get(uid)
            item = dict(
                kind=record_kind,
                id=row.id,
                user_id=uid,
                owner=user.display_name if user else "",
                freeman_id=user.freeman_id if user else None,
                registration_source=user.registration_source if user else None,
                avatar=f"/api/web/admin/users/{uid}/avatar"
                if user and user.telegram_id
                else None,
            )
            if record_kind in ("client", "key", "proxy"):
                item.update(
                    label=user.display_name
                    if record_kind == "client"
                    else named.get(row.id,row.name)
                    if record_kind == "key"
                    else f"Proxy #{row.id}",
                    detail=row.status,
                )
            else:
                item.update(
                    label=f"{'Платёж P' if record_kind == 'payment' else 'Операция B' if record_kind == 'balance' else 'Заказ O'}{row.id}",
                    detail=row.status
                    if record_kind == "payment"
                    else row.type
                    if record_kind == "balance"
                    else "Заказ покупки",
                    payment_id=row.id
                    if record_kind == "payment"
                    else row.payment_id
                    if record_kind == "order"
                    and row.payment_id in visible_order_payments
                    else None,
                )
            if record_kind == "proxy":
                item["server_id"] = row.server_id
            items.append(item)
    return {
        "items": items,
        "truncated": any(len(rows) > LIMIT for _, rows in groups),
        "limit_per_type": LIMIT,
    }
