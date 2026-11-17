"""Confirmed, durable key operations. Repository writes commit before external IO.

A proposal is bound to a conversation and exact authoritative snapshot. A crash
or unknown network outcome never causes a second revoke. Delivery is separate.
"""
import secrets
import time

class ActionDenied(ValueError):
    pass

class ActionService:
    def __init__(self, repository, panel, *, clock=time.time):
        self.repo, self.panel, self.clock = repository, panel, clock

    async def prepare(self, subject, key, kind, devices):
        if kind not in ('detach', 'reissue'):
            raise ActionDenied('unsupported_action')
        async with self.repo.lock(str(key)):
            pending=await self.repo.active(key)
            if pending:
                previous=await self.repo.get(pending)
                if not previous or previous['status']!='done':raise ActionDenied('previous_operation_unresolved')
                await self.repo.set_active(key,'')
            snapshot = await self.panel.snapshot(key)
            if kind == 'detach' and (not devices or len(set(devices)) != len(devices)
                                    or not set(devices) <= set(snapshot['devices'])):
                raise ActionDenied('invalid_device_selection')
            token = secrets.token_urlsafe(24)
            await self.repo.put(token, dict(subject=subject, key=key, kind=kind,
                devices=devices, before=snapshot, status='prepared', expires=self.clock()+900))
            return token

    async def read(self, token, subject):
        row = await self.repo.get(token)
        if not row or row['subject'] != subject:
            raise ActionDenied('action_unavailable')
        if row['status'] == 'canceled' or row['status'] == 'prepared' and row['expires'] < self.clock():
            raise ActionDenied('action_expired_or_canceled')
        return row

    async def cancel(self, token, subject):
        row = await self.read(token, subject)
        async with self.repo.lock(str(row['key'])):
            row = await self.read(token, subject)
            if row['status'] != 'prepared':
                raise ActionDenied('operation_already_started')
            row['status'] = 'canceled'
            await self.repo.put(token, row)

    async def confirm(self, token, subject):
        initial = await self.read(token, subject)
        async with self.repo.lock(str(initial['key'])):
            row = await self.read(token, subject)
            if row['status'] == 'done':
                return row['result']
            active=await self.repo.active(row['key'])
            if active and active!=token:raise ActionDenied('previous_operation_unresolved')
            before = row['before']
            current = await self.panel.snapshot(row['key'])
            if row['status'] == 'prepared':
                if current != before:
                    raise ActionDenied('snapshot_changed_confirm_again')
                # Must be durable before touching the provider, even if caller crashes.
                row['status'] = 'executing'
                await self.repo.start(row['key'],token,row)
                try:
                    if row['kind'] == 'reissue':
                        await self.panel.reissue(row['key'])
                    else:
                        await self.panel.detach(row['key'], row['devices'])
                except Exception:
                    # Never log exception bodies (provider may include key/credentials).
                    row['status'] = 'uncertain'
                    await self.repo.put(token, row)
                try:
                    current = await self.panel.snapshot(row['key'])
                except Exception:
                    return dict(status='uncertain', operation=token)
            invariants = all(current.get(k) == before.get(k) for k in ('expiry','tariff','limit'))
            if row['kind'] == 'reissue':
                confirmed = bool(current.get('link') and current['link'] != before['link'])
            else:
                confirmed = current['link']==before['link'] and not (set(row['devices']) & set(current['devices']))
            if confirmed and invariants:
                await self.panel.associate(row['key'], current)
                result = dict(status='done', snapshot=current, operation=token)
                row.update(status='done', result=result)
                await self.repo.put(token, row)
                await self.repo.set_active(row['key'],'')
                return result
            row['status'] = 'uncertain'
            await self.repo.put(token, row)
            return dict(status='uncertain', operation=token,
                        remaining=[d for d in current['devices'] if d in row['devices']])
