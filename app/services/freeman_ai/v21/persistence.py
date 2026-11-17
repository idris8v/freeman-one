"""Existing PostgreSQL/settings and RemnaWave adapters; no second customer DB."""
import json
import re
from contextlib import asynccontextmanager
from sqlalchemy import or_, select
from app.database.models import AIConversation,AIMessage,VlessCredential,Subscription,AppSetting,AIEvent,Device,now
from app.database.repositories import SQLRepository
from app.services.freeman_ai.security import encrypt,decrypt,fingerprint,aware
from .actions import ActionDenied

class DurableRepository:
    def __init__(self,sessions,settings):self.sessions,self.settings=sessions,settings
    def key(self,token):
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}',token):raise ActionDenied('invalid_action_token')
        return 'agent_v21_action:'+fingerprint(self.settings,token)
    async def get(self,token):
        async with self.sessions() as s:
            key=self.key(token)
            parts=dict((await s.execute(select(AppSetting.key,AppSetting.value).where(
                or_(AppSetting.key==key,AppSetting.key.startswith(key+':',autoescape=True)),
            ))).all())
            raw=parts.get(key)
            if raw and raw.startswith('chunks:'):
                count=int(raw.partition(':')[2])
                raw=''.join(parts[f'{key}:{i}'] for i in range(count))
            return json.loads(decrypt(self.settings,raw)) if raw else None

    async def write(self,store,token,row):
        key=self.key(token)
        encrypted=encrypt(self.settings,json.dumps(row,ensure_ascii=False))
        chunks=[encrypted[i:i+4000] for i in range(0,len(encrypted),4000)]
        if len(chunks)==1:
            await store.set_setting(key,chunks[0])
        else:
            for i,chunk in enumerate(chunks):
                await store.set_setting(f'{key}:{i}',chunk)
            await store.set_setting(key,f'chunks:{len(chunks)}')

    async def put(self,token,row):
        async with self.sessions.begin() as s:
            await self.write(SQLRepository(s),token,row)

    async def start(self,key,token,row):
        async with self.sessions.begin() as s:
            store=SQLRepository(s)
            await self.write(store,token,row)
            await store.set_setting('agent_v21_active_operation:'+str(key),encrypt(self.settings,token))
    async def active(self,key):
        async with self.sessions() as s:
            raw=await SQLRepository(s).setting('agent_v21_active_operation:'+str(key))
            return decrypt(self.settings,raw) if raw else ''
    async def set_active(self,key,token):
        async with self.sessions.begin() as s:
            await SQLRepository(s).set_setting('agent_v21_active_operation:'+str(key),encrypt(self.settings,token) if token else '')
    @asynccontextmanager
    async def lock(self,key):
        # Same subscription-row lock used by HWID cache and renewal. Journal
        # commits use another session so crash markers survive remote requests.
        async with self.sessions.begin() as s:
            sid=await s.scalar(select(Subscription.id).join(VlessCredential,VlessCredential.subscription_id==Subscription.id).where(VlessCredential.id==int(key)).with_for_update(of=Subscription))
            if sid is None:raise ActionDenied('key_unavailable')
            try:
                yield
            finally:
                from app.services.vless_hwid import advance
                await advance(SQLRepository(s),sid)

class PanelAdapter:
    def __init__(self,sessions,settings,cid,history=(),uid=None):
        self.sessions,self.settings,self.cid,self.history,self.uid=sessions,settings,cid,history,uid
    @asynccontextmanager
    async def context(self,key,*,without_panel=False):
        from .backend import Backend
        async with self.sessions.begin() as s:
            conv=await s.get(AIConversation,self.cid)
            if not conv or conv.mode!='ai':raise ActionDenied('conversation_not_automated')
            # Trust persisted client messages, not data submitted in a callback.
            history=list(await s.scalars(select(AIMessage).where(AIMessage.conversation_id==conv.id,AIMessage.role=='client').order_by(AIMessage.id.desc()).limit(100)))
            backend=Backend(s,self.settings,conv,history,verified_user=self.uid)
            c=await backend.resolve('key:'+str(key));api=None if without_panel else await backend.panel(c)
            yield s,backend,c,api
    async def snapshot(self,key):
        from app.services.remnawave_contract import user
        async with self.context(key) as (s,b,c,api):
            remote=user(await api.user(c.remote_user_uuid),c.remote_user_uuid)
            devices=await b.device_data(c)
            sub=await s.get(Subscription,c.subscription_id)
            if not remote.get('expireAt') or type(remote.get('hwidDeviceLimit')) is not int:
                raise ValueError('incomplete_panel_snapshot')
            from .registry import key_link
            link=remote.get('subscriptionUrl')
            if not key_link(link):
                raise ValueError('unsupported_panel_subscription_url')
            return dict(link=link,
                expiry=remote.get('expireAt'),limit=remote.get('hwidDeviceLimit'),tariff=sub.tariff_id,
                short_uuid=remote.get('shortUuid'),
                devices=sorted(ref for ref,_ in devices),device_names={ref:(d.get('deviceModel') or 'Устройство')[:100] for ref,d in devices})
    async def detach(self,key,refs):
        async with self.context(key) as (s,b,c,api):
            devices=dict(await b.device_data(c))
            if not set(refs)<=set(devices):raise ActionDenied('devices_changed')
            # Selected HWIDs only. Even “all” means the snapshot shown to client;
            # devices connected later are never silently included.
            for ref in refs:await api.delete_device(devices[ref]['hwid'],c.remote_user_uuid)
    async def reissue(self,key):
        async with self.context(key) as (s,b,c,api):
            await api.revoke_user(c.remote_user_uuid)
    async def associate(self,key,snapshot):
        from .registry import key_link
        async with self.context(key) as (s,b,c,api):
            if not key_link(snapshot['link']):raise ValueError('unexpected_panel_subscription_url')
            old=c.subscription_url
            c.subscription_url=snapshot['link'];c.last_sync=now()
            if isinstance(snapshot.get('short_uuid'),str):c.short_uuid=snapshot['short_uuid']
            # Key proof follows exactly one confirmed reissue in this chat only.
            if old!=c.subscription_url:
                await b.repo.set_setting(f'agent_v21_proof:{self.cid}:{c.id}',encrypt(self.settings,fingerprint(self.settings,c.subscription_url)))
            live=set(snapshot['devices'])
            for d in await s.scalars(select(Device).where(Device.subscription_id==c.subscription_id,Device.status.in_(('active','connected','online')))):
                if d.provisioning_id and b.device_ref(c,d.provisioning_id) not in live:d.status='removed'
            s.add(AIEvent(conversation_id=self.cid,kind='agent.key_operation',name=str(c.id),status='confirmed'))
