"""Confirmed, resumable managed-access cleanup. No financial records are deleted."""
import hashlib
import json
from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update, String
from cryptography.fernet import InvalidToken
from app.database.models import AppSetting, AdminAudit, Payment, ProxyCredential, ProxyIPSeen, ProxyServer, now
from app.services.proxies import ProxyService
from app.services.telemt import TelemtError


class RemovalInput(BaseModel):
    confirmed: bool = False
    confirmation: str = Field(default='', max_length=80)
    revision: str = Field(min_length=64, max_length=64, pattern='^[a-f0-9]+$')
    mode: str = Field(default='revoke', pattern='^(revoke|local_only)$')
    acknowledge_local_only: bool = False


def marker(sid): return f'proxy_server_removal:{sid}'


async def removal_running(repo, sid):
    data = json.loads(await repo.setting(marker(sid), '{}'))
    return bool(data and not data.get('complete'))


async def invitations(repo, sid, lock=False):
    # Claim locks the invitation before provision locks the server. Use the same
    # order here; never hold the server while waiting on a claiming invitation.
    found = []
    for row in await repo.session.scalars(select(AppSetting).where(AppSetting.key.startswith('proxy_invite:')).order_by(AppSetting.key)):
        try: data = json.loads(row.value)
        except (ValueError, TypeError): continue
        if data.get('server_id') == sid and data.get('status') in ('open', 'claiming'):
            found.append((row, data))
    if lock:
        for row, _ in found:
            await repo.session.refresh(row, with_for_update=True)
        found = [(row, json.loads(row.value)) for row, _ in found
                 if json.loads(row.value).get('status') in ('open', 'claiming')]
    return found


async def inspect_removal(repo, item):
    rows = list(await repo.session.scalars(select(ProxyCredential).where(
        ProxyCredential.server_id == item.id, ProxyCredential.status != 'deleted').order_by(ProxyCredential.id)))
    invites = await invitations(repo, item.id)
    ids = {c.id for c in rows}
    pending = 0
    for payment, raw in await repo.session.execute(select(Payment, AppSetting.value).join(
        AppSetting, AppSetting.key == 'proxy_order:' + Payment.id.cast(String)).where(
            Payment.status.in_(('proxy_order_created', 'proxy_order_pending', 'proxy_order_processing')))):
        try: order = json.loads(raw)
        except (ValueError, TypeError): continue
        if order.get('credential_id') in ids: pending += 1
    state = json.loads(await repo.setting(marker(item.id), '{}'))
    version = [item.id, item.status, [(c.id, c.status, c.remote_username) for c in rows],
               [(row.key, data.get('status')) for row, data in invites]]
    revision = hashlib.sha256(json.dumps(version, sort_keys=True).encode()).hexdigest()
    total = max(len(rows), state.get('total', len(rows)))
    local = state.get('mode') == 'local_only'
    return dict(id=item.id, name=item.name, remaining=len(rows), total=total, removed=max(0, total-len(rows)),
                invitations=len(invites), pending_payments=pending, revision=revision,
                started=bool(state), complete=item.status == 'deleted' and not rows,
                remote_revoked=not local, mode=state.get('mode', 'revoke'),
                warning=('Записи удалены только из Freeman. Отзыв на старом сервере не подтверждён. '
                         'Остановите Telemt на старом сервере, иначе прежние ссылки могут работать.') if local else '')


async def remove_batch(repo, settings, actor, sid, payload):
    # Invitation claims use invitation -> server locks, so preserve that order.
    invites = await invitations(repo, sid, lock=True)
    item = await repo.session.scalar(select(ProxyServer).where(ProxyServer.id == sid).with_for_update())
    if not item: raise HTTPException(404, 'Сервер не найден')
    view = await inspect_removal(repo, item)
    if not payload.confirmed or payload.confirmation != item.name:
        raise HTTPException(409, 'Для подтверждения введите точное название сервера')
    if payload.mode == 'local_only' and not payload.acknowledge_local_only:
        raise HTTPException(409, 'Подтвердите, что доступ на старом сервере не будет отозван')
    if view['complete']: return dict(view, ok=True)
    if payload.revision != view['revision']:
        raise HTTPException(409, 'Список подключений изменился. Откройте удаление заново и проверьте количество')
    # Protect renewal invoices that can still be paid; never cancel a payment or
    # destroy the fulfillment target as a side effect of server cleanup.
    if view['pending_payments']:
        raise HTTPException(409, 'Есть незавершённые платежи продления. Дождитесь завершения или отмены счетов')
    state = json.loads(await repo.setting(marker(sid), '{}'))
    if not state:
        item.status = 'maintenance'
        await repo.set_setting(marker(sid), json.dumps(dict(total=view['remaining'], mode=payload.mode, complete=False)))
        await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id or -actor.id,
            action='proxy_control_server_removal_start', target=str(sid), details=f'issued={view["remaining"]}; mode={payload.mode}'))
        # Commit maintenance before any network call. New provision() refreshes
        # this locked row and refuses allocation. This also makes retries safe.
        await repo.session.flush()
        return dict(await inspect_removal(repo, item), ok=True)
    item.status = 'maintenance'
    rows = list(await repo.session.scalars(select(ProxyCredential).where(
        ProxyCredential.server_id == sid, ProxyCredential.status != 'deleted').order_by(ProxyCredential.id).with_for_update()))
    batch = rows[:5]
    error = ''
    if payload.mode == 'revoke' and batch:
        try:
            api = ProxyService(repo, settings).api(item)
            api.timeout = min(api.timeout, 4)
            health = await api.request('GET', '/v1/health')
            if not isinstance(health, dict) or health.get('read_only'):
                raise ValueError('API is not writable')
            for c in batch:
                try: await api.delete_user(c.remote_username)
                except TelemtError as exc:
                    if exc.status != 404: raise
            users = await api.users()
            if not isinstance(users, list) or any(not isinstance(u, dict) or not u.get('username') for u in users):
                raise ValueError('Invalid users snapshot')
            present = {u['username'] for u in users}
            batch = [c for c in batch if c.remote_username not in present]
        except (ValueError, TelemtError, InvalidToken):
            batch = []
            error = 'Не удалось подтвердить отзыв на Telemt. Выдача остановлена. Повторите попытку или явно выберите удаление только из Freeman.'
    for c in batch:
        c.status = 'deleted'; c.link = ''; c.current_connections = 0; c.active_unique_ips = 0; c.last_sync = now()
    if batch:
        await repo.session.execute(update(ProxyIPSeen).where(ProxyIPSeen.credential_id.in_([c.id for c in batch])).values(active=False))
    for row, data in invites:
        data['status'] = 'revoked'; row.value = json.dumps(data)
    remaining = len(rows) - len(batch)
    complete = not remaining
    state.update(mode=payload.mode, complete=complete)
    await repo.set_setting(marker(sid), json.dumps(state))
    if complete:
        item.status = 'deleted'; item.current_connections = 0; item.active_users = 0; item.remote_users = 0
    if batch or complete:
        await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id or -actor.id,
            action='proxy_control_server_removal_done' if complete else 'proxy_control_server_removal_batch',
            target=str(sid), details=f'removed={len(batch)}; remaining={remaining}; mode={payload.mode}'))
    await repo.session.flush()
    return dict(await inspect_removal(repo, item), ok=not error, error=error)
