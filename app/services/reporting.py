"""Safe CSV/XLSX management exports. No VPN configurations or payment secrets."""
from __future__ import annotations

import csv
import json
import io
from collections import defaultdict
from datetime import timedelta, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select

from app.database.models import (
    AccessKey,
    Payment,
    Subscription,
    Tariff,
    User,
    VpnCredential,
    VpnServer,
    now,
)
from app.services.screens import duration


PERIODS = {1: "today", 7: "7d", 30: "30d", 90: "90d", 0: "all"}


def safe_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def csv_file(headers, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(headers)
    writer.writerows([[safe_cell(value) for value in row] for row in rows])
    return output.getvalue().encode("utf-8-sig")


class ReportingService:
    def __init__(self, repo):
        self.repo = repo
        self.session = repo.session

    def since(self, days):
        if days not in PERIODS:
            raise ValueError("Период недоступен.")
        return now() - timedelta(days=days) if days else None

    @staticmethod
    def active(expiry):
        return (expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry) > now()

    async def archived(self):
        return {
            int(key.split(":", 1)[1])
            for key, value in (await self.repo.settings_prefix("user_archived:")).items()
            if value == "1" and key.split(":", 1)[1].isdigit()
        }

    async def names(self):
        tariffs = list(await self.session.scalars(select(Tariff)))
        result = {}
        for tariff in tariffs:
            result[tariff.id] = await self.repo.tariff_setting(tariff, "tariff_name", duration(tariff.months))
        return result

    async def datasets(self, days):
        since = self.since(days)
        archived = await self.archived()
        tariff_names = await self.names()

        users_query = select(User).order_by(User.id)
        if archived:
            users_query = users_query.where(User.id.not_in(archived))
        users = list(await self.session.scalars(users_query))
        users_by_id = {item.id: item for item in users}

        subscriptions_query = select(Subscription).order_by(Subscription.id)
        keys_query = select(AccessKey).order_by(AccessKey.id)
        payments_query = select(Payment).order_by(Payment.id)
        if archived:
            subscriptions_query = subscriptions_query.where(Subscription.user_id.not_in(archived))
            keys_query = keys_query.where(AccessKey.user_id.not_in(archived))
            payments_query = payments_query.where(Payment.user_id.not_in(archived))
        subscriptions = list(await self.session.scalars(subscriptions_query))
        keys = list(await self.session.scalars(keys_query))
        if since:
            payments_query = payments_query.where(Payment.created_at >= since)
        payments = list(await self.session.scalars(payments_query))

        subscription_by_id = {item.id: item for item in subscriptions}
        subscriptions_by_user = defaultdict(list)
        keys_by_user = defaultdict(list)
        for item in subscriptions:
            subscriptions_by_user[item.user_id].append(item)
        for item in keys:
            keys_by_user[item.user_id].append(item)

        all_sales = list(
            await self.session.scalars(
                select(Payment).where(Payment.status.in_(("succeeded", "order_paid", "stars_paid", "refunded", "balance_paid", "manual_paid")))
            )
        )
        paid_by_user = defaultdict(int)
        for payment in all_sales:
            if payment.user_id not in archived:
                paid_by_user[payment.user_id] += payment.amount if payment.status in ("succeeded", "order_paid", "stars_paid", "balance_paid", "manual_paid") else 0

        client_rows = []
        for user in users:
            user_subscriptions = subscriptions_by_user[user.id]
            active = sum(self.active(item.expires_at) for item in user_subscriptions)
            balance = await self.repo.balance(user.id)
            client_rows.append([
                user.id,
                user.telegram_id,
                "@" + user.username if user.username else "",
                user.display_name,
                user.created_at.isoformat(),
                user.status,
                "да" if user.trial_used else "нет",
                len(user_subscriptions),
                active,
                len([key for key in keys_by_user[user.id] if key.status != "deleted"]),
                f"{paid_by_user[user.id] / 100:.2f}",
                f"{balance / 100:.2f}",
                user.freeman_id,user.account_username or "",user.email or "",
                "да" if user.email_verified else "нет",user.registration_source,
            ])

        subscription_rows = []
        for item in subscriptions:
            user = users_by_id.get(item.user_id)
            if not user:
                continue
            subscription_rows.append([
                item.id,
                user.telegram_id,
                "@" + user.username if user.username else "",
                tariff_names.get(item.tariff_id, "Пробный период" if item.kind == "trial" else "Без тарифа"),
                item.kind,
                item.created_at.isoformat(),
                item.expires_at.isoformat(),
                "активна" if self.active(item.expires_at) else "истекла",
                item.device_limit,
            ])

        credential_rows = []
        credentials = list(await self.session.scalars(select(VpnCredential).order_by(VpnCredential.id)))
        servers = {item.id: item for item in await self.session.scalars(select(VpnServer))}
        credentials_by_key = {item.access_key_id: item for item in credentials if item.access_key_id}
        for item in keys:
            user = users_by_id.get(item.user_id)
            if not user:
                continue
            credential = credentials_by_key.get(item.id)
            server = servers.get(credential.server_id) if credential else None
            credential_rows.append([
                item.id,
                user.telegram_id,
                "@" + user.username if user.username else "",
                item.name,
                item.mode,
                item.status,
                item.created_at.isoformat(),
                item.expires_at.isoformat(),
                server.name if server else "",
                server.country if server else "",
                credential.protocol if credential else "",
            ])

        payment_rows = []
        tariff_summary = defaultdict(lambda: [0, 0, 0, set()])
        daily_summary = defaultdict(lambda: [0, 0, 0])
        for item in payments:
            user = users_by_id.get(item.user_id)
            if not user:
                continue
            subscription = subscription_by_id.get(item.subscription_id)
            snapshot=json.loads(await self.repo.setting(f"sale_snapshot:{item.id}","{}"))
            tariff_name = snapshot.get("tariff_name") or (tariff_names.get(subscription.tariff_id, "Без тарифа") if subscription else "Пополнение баланса")
            payment_rows.append([
                item.id,
                item.created_at.isoformat(),
                user.telegram_id,
                "@" + user.username if user.username else "",
                item.subscription_id or "",
                tariff_name,
                item.status,
                f"{item.amount / 100:.2f}",
                item.provider_id or "",
            ])
            if item.status in ("succeeded", "order_paid", "stars_paid", "refunded", "balance_paid", "manual_paid"):
                summary = tariff_summary[tariff_name]
                summary[0] += 1
                summary[1] += item.amount
                summary[3].add(item.user_id)
                if item.status == "refunded":
                    summary[2] += item.amount
                day = item.created_at.date().isoformat()
                daily_summary[day][0] += 1
                daily_summary[day][1] += item.amount
                if item.status == "refunded":
                    daily_summary[day][2] += item.amount

        tariff_rows = [
            [name, values[0], len(values[3]), f"{values[1]/100:.2f}", f"{values[2]/100:.2f}", f"{(values[1]-values[2])/100:.2f}"]
            for name, values in sorted(tariff_summary.items(), key=lambda pair: pair[1][1] - pair[1][2], reverse=True)
        ]
        daily_rows = [
            [day, values[0], f"{values[1]/100:.2f}", f"{values[2]/100:.2f}", f"{(values[1]-values[2])/100:.2f}"]
            for day, values in sorted(daily_summary.items())
        ]
        return {
            "Клиенты": (["ID", "Telegram ID", "Username", "Имя", "Регистрация UTC", "Статус", "Пробный использован", "Подписок", "Активных", "Ключей", "Оплачено, ₽", "Баланс, ₽", "Freeman ID", "Логин сайта", "Email", "Email подтверждён", "Источник"], client_rows),
            "Подписки": (["ID", "Telegram ID", "Username", "Тариф", "Тип", "Создана UTC", "Действует до UTC", "Состояние", "Лимит устройств"], subscription_rows),
            "Ключи": (["ID", "Telegram ID", "Username", "Название", "Режим", "Статус", "Создан UTC", "Действует до UTC", "Сервер", "Страна", "Протокол"], credential_rows),
            "Платежи": (["ID", "Дата UTC", "Telegram ID", "Username", "Подписка", "Тариф/операция", "Статус", "Сумма, ₽", "ID провайдера"], payment_rows),
            "По тарифам": (["Тариф", "Продаж", "Покупателей", "Оборот, ₽", "Возвраты, ₽", "Чистая выручка, ₽"], tariff_rows),
            "По дням": (["Дата UTC", "Платежей", "Оборот, ₽", "Возвраты, ₽", "Чистая выручка, ₽"], daily_rows),
        }

    async def xlsx(self, days):
        datasets = await self.datasets(days)
        workbook = Workbook()
        workbook.remove(workbook.active)
        header_fill = PatternFill("solid", fgColor="172554")
        for title, (headers, rows) in datasets.items():
            sheet = workbook.create_sheet(title)
            sheet.append(headers)
            for row in rows:
                sheet.append([safe_cell(value) for value in row])
            for cell in sheet[1]:
                cell.font = Font(color="FFFFFF", bold=True)
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center")
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for column in range(1, len(headers) + 1):
                values = [str(sheet.cell(row=row, column=column).value or "") for row in range(1, min(sheet.max_row, 250) + 1)]
                sheet.column_dimensions[get_column_letter(column)].width = min(max(map(len, values), default=10) + 2, 42)
        output = io.BytesIO()
        workbook.save(output)
        return output.getvalue()
