"""Client-confirmed replacement of both subscription URL and VPN credentials."""
import hashlib,json,secrets
from datetime import timezone
from sqlalchemy import select,update
from app.database.models import User,AccessKey,VlessCredential,AdminAudit,now
from app.services.screens import Screen
from app.services.vless_provision import control,connection

def fingerprint(value):return hashlib.sha256(value.encode()).hexdigest()
async def render(repo,settings,user,action):
    p=action.split(':');confirm=p[1]=='confirm'
    if confirm:sid=int(p[2]);data=None
    elif p[1]=='apply':
        await repo.session.execute(update(User).where(User.id==user.id).values(status=User.status))
        data=json.loads(await repo.setting('client_vless_rotate:'+p[2],'{}'))
        if data.get('actor')!=user.id or data.get('done') or data.get('expires',0)<now().timestamp():raise ValueError('Подтверждение устарело или уже использовано.')
        sid=data['sid']
    else:raise ValueError('Действие недоступно.')
    sub=await repo.subscription(user.id,sid)
    c=await repo.session.scalar(select(VlessCredential).where(VlessCredential.subscription_id==sid,VlessCredential.user_id==user.id)) if sub else None
    if not c or c.status!='active' or await repo.setting(f'subscription_removed:{sid}')=='1':raise ValueError('Подписка недоступна.')
    key=await repo.session.get(AccessKey,c.access_key_id)
    if not key or key.status!='active' or await repo.setting(f'subscription_status:{sid}','active')!='active':raise ValueError('Доступ к подписке приостановлен.')
    if sub.expires_at.replace(tzinfo=timezone.utc)<=now():raise ValueError('Сначала продлите подписку.')
    if confirm:
        token=secrets.token_hex(12)
        await repo.set_setting('client_vless_rotate:'+token,json.dumps({'actor':user.id,'sid':sid,'cid':c.id,'fingerprint':fingerprint(c.subscription_url),'expires':now().timestamp()+300}))
        target='vr:apply:'+token
        return Screen('🔐 <b>Перевыпустить ключ?</b>\n\nСтарая ссылка и параметры VPN будут отозваны. Все устройства потребуется подключить с новым ключом.\n\nВладелец, срок подписки и лимит устройств сохранятся. Оплата не требуется.',[[('Да, перевыпустить',target)],[('← Отмена',f'subscription_extra:{sid}')]],button_options={target:{'style':'danger'}})
    if c.id!=data['cid'] or fingerprint(c.subscription_url)!=data['fingerprint']:raise ValueError('Ключ уже изменён. Откройте карточку заново.')
    await control(repo,settings,c,'rotate')
    data['done']=True;await repo.set_setting('client_vless_rotate:'+p[2],json.dumps(data))
    await repo.add(AdminAudit(actor_telegram_id=user.telegram_id or 0,action='client_vless_rotate',target=str(c.access_key_id),details='URL and VPN credentials revoked; expiry and device limit preserved'))
    screen=await connection(repo,settings,sub,user)
    screen.appearance_page='rotate_result'
    screen.text='✅ <b>Ключ перевыпущен</b>\nСтарая ссылка и параметры подключения отозваны. Установите новый ключ на ваших устройствах.\n\n'+screen.text
    return screen
