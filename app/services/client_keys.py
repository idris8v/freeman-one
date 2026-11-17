import json
import secrets
from datetime import timedelta, datetime
from sqlalchemy import update, select
from app.database.models import User,AccessKey,AdminAudit,now
from app.services.keys import KeyService
from app.services.screens import Screen


async def prepare_client_thaws(repo, settings=None):
    """Release only client freezes; preserve expiry and administrator restrictions."""
    current=now()
    keys=list(await repo.session.scalars(select(AccessKey).where(AccessKey.status=='frozen')))
    for key in keys:
        if await repo.setting(f'client_frozen:{key.id}')!='1':
            continue
        raw=await repo.setting(f'client_freeze_until:{key.id}')
        if not raw:
            days=max(1,min(3,int(await repo.setting('client_freeze_days','3'))))
            await repo.set_setting(f'client_freeze_until:{key.id}',(current+timedelta(days=days)).isoformat())
            continue
        if datetime.fromisoformat(raw)>current:
            continue
        await repo.session.execute(update(User).where(User.id==key.user_id).values(status=User.status))
        await repo.session.refresh(key)
        raw=await repo.setting(f'client_freeze_until:{key.id}')
        if key.status!='frozen' or await repo.setting(f'client_frozen:{key.id}')!='1' or not raw or datetime.fromisoformat(raw)>current:
            continue
        key.status='active'
        await repo.set_setting(f'client_frozen:{key.id}','0')
        await repo.set_setting(f'client_freeze_until:{key.id}','')
        for sub in await repo.subscriptions(key.user_id):
            if await repo.setting(f'subscription_key:{sub.id}')==str(key.id) and await repo.setting(f'subscription_status:{sub.id}')=='frozen':
                if key.mode!='mock' and settings:
                    from app.services.vpn import VpnService
                    await VpnService(repo,settings).set_subscription_status(sub.id,'active')
                await repo.set_setting(f'subscription_status:{sub.id}',await repo.setting(f'client_freeze_prev:{sub.id}','active'))
        await repo.add(AdminAudit(actor_telegram_id=0,action='client_key_auto_thaw',target=str(key.id),details='Автоматическое размораживание; MOCK; срок не менялся'))


class ClientKeyService:
    def __init__(self,repo,settings):
        self.repo,self.settings=repo,settings

    async def render(self,action,user):
        p=action.split(':')
        if p[0]=='sub_control':
            if len(p)!=3 or p[1]!='delete':
                raise ValueError('Действие недоступно.')
            sub=await self.repo.subscription(user.id,int(p[2]))
            if not sub or await self.repo.setting(f'subscription_removed:{sub.id}')=='1':
                raise ValueError('Подписка недоступна.')
            token=secrets.token_hex(8)
            await self.repo.set_setting('client_sub_confirm:'+token,json.dumps({'actor':user.id,'sub':sub.id,'expires':now().timestamp()+300}))
            return Screen('Удалить подписку и безвозвратно отозвать связанный ключ? Подключение перестанет работать. Деньги и оставшийся срок не возвращаются.',[[('🗑 Да, удалить и отозвать ключ',f'sub_apply:{token}')],[('Отмена',f'subscription_extra:{sub.id}')]])
        if p[0]=='sub_apply':
            await self.repo.session.execute(update(User).where(User.id==user.id).values(status=User.status))
            data=json.loads(await self.repo.setting('client_sub_confirm:'+p[1],'{}'))
            if data.get('actor')!=user.id or data.get('expires',0)<now().timestamp() or data.get('done'):
                raise ValueError('Подтверждение недоступно или уже использовано.')
            sub=await self.repo.subscription(user.id,int(data['sub']))
            if not sub or await self.repo.setting(f'subscription_removed:{sub.id}')=='1':
                raise ValueError('Подписка уже удалена.')
            key_id=await self.repo.setting(f'subscription_key:{sub.id}')
            if key_id:
                key=await KeyService(self.repo,self.settings).owned(user.id,int(key_id))
                if key:
                    if key.mode!='mock':
                        from app.services.vpn import VpnService
                        await VpnService(self.repo,self.settings).set_subscription_status(sub.id,'deleted')
                    key.status='deleted'
            await self.repo.set_setting(f'subscription_removed:{sub.id}','1')
            await self.repo.set_setting(f'auto_renew:{sub.id}','0')
            data['done']=True
            await self.repo.set_setting('client_sub_confirm:'+p[1],json.dumps(data))
            await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id,action='client_subscription_delete',target=str(sub.id),details='Скрыто клиентом; автопродление выключено'))
            return Screen('Подписка удалена, связанный ключ отозван.',[[('📦 Мои подписки','subscriptions')]])
        if p[0]=='key_control':
            if len(p)!=3 or p[1]!='delete':
                raise ValueError('Действие недоступно.')
            key=await KeyService(self.repo,self.settings).owned(user.id,int(p[2]))
            if not key or key.status not in ('active','frozen','billing_blocked'):
                raise ValueError('Действие недоступно.')
            token=secrets.token_hex(8)
            await self.repo.set_setting('client_key_confirm:'+token,json.dumps({'actor':user.id,'key':key.id,'operation':'delete','status':key.status,'mode':key.mode,'expires':now().timestamp()+300}))
            return Screen('Удалить ключ? Он станет недоступен. Действие необратимо, деньги и оставшийся срок не возвращаются.',[[('✅ Подтвердить','key_apply:'+token)],[('Отмена',f'keydetail:{key.id}:0')]])
        if p[0]!='key_apply' or len(p)!=2:
            raise ValueError('Действие недоступно.')
        await self.repo.session.execute(update(User).where(User.id==user.id).values(status=User.status))
        token=p[1];data=json.loads(await self.repo.setting('client_key_confirm:'+token,'{}'))
        if data.get('actor')!=user.id or data.get('expires',0)<now().timestamp():
            raise ValueError('Подтверждение недоступно или устарело.')
        if data.get('done'):
            return Screen('Действие уже выполнено.',[[('📦 Мои подписки','subscriptions')]])
        operation,key_id=data.get('operation'),data.get('key')
        if operation!='delete':
            raise ValueError('Действие недоступно.')
        key=await KeyService(self.repo,self.settings).owned(user.id,key_id)
        if not key or key.status not in ('active','frozen','billing_blocked'):
            return Screen('Ключ уже изменён. Откройте подписку заново.',[[('📦 Мои подписки','subscriptions')]])
        linked=[]
        for sub in await self.repo.subscriptions(user.id):
            if await self.repo.setting(f'subscription_key:{sub.id}')==str(key_id): linked.append(sub)
        if key.mode!='mock':
            from app.services.vpn import VpnService
            for sub in linked:
                await VpnService(self.repo,self.settings).set_subscription_status(sub.id,'deleted')
        result=await self.repo.session.execute(update(AccessKey).where(AccessKey.id==key_id,AccessKey.user_id==user.id,AccessKey.status==data['status'],AccessKey.mode==data.get('mode',key.mode)).values(status='deleted').execution_options(synchronize_session='fetch'))
        if not result.rowcount:
            return Screen('Ключ уже изменён. Откройте подписку заново.',[[('📦 Мои подписки','subscriptions')]])
        await self.repo.set_setting(f'client_frozen:{key_id}','0')
        await self.repo.set_setting(f'client_freeze_until:{key_id}','')
        for sub in linked:
            await self.repo.set_setting(f'subscription_status:{sub.id}','deleted')
            await self.repo.set_setting(f'subscription_removed:{sub.id}','1')
            await self.repo.set_setting(f'auto_renew:{sub.id}','0')
        data['done']=True
        await self.repo.set_setting('client_key_confirm:'+token,json.dumps(data))
        await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id,action='client_key_'+operation,target=str(key_id),details=f'Клиентское управление; {key.mode}; срок не менялся'))
        return Screen('Ключ удалён. Возврат средств не выполняется.',[[('📦 Мои подписки','subscriptions')]])
