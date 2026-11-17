"""Build only the visible five key labels, using owner-scoped batched settings."""
from sqlalchemy import select
from app.database.models import AccessKey,now
from app.services.vless_creation import utc
from app.services.screens import Screen,time_remaining
from app.bot.keyboards import back

async def render(service,user,offset=0):
    repo=service.repo;subs=await repo.subscriptions(user.id)
    keys=list(await repo.session.scalars(select(AccessKey).where(AccessKey.user_id==user.id,AccessKey.status!='deleted').order_by(AccessKey.id.desc())))
    await repo.prefetch_settings(keys=[f'{prefix}:{s.id}' for s in subs for prefix in ('subscription_key','subscription_removed','subscription_transferred','subscription_name','subscription_status')])
    known={k.id:k for k in keys};linked=set();mapping={}
    for sub in subs:
        if await repo.setting(f'subscription_transferred:{sub.id}')=='1' or await repo.setting(f'subscription_removed:{sub.id}')=='1':
            linked.add(sub.id);continue
        kid=await repo.setting(f'subscription_key:{sub.id}')
        if kid and kid.isdigit() and int(kid) in known:linked.add(sub.id);mapping[int(kid)]=sub
    entries=[(mapping.get(k.id),k) for k in keys]+[(s,None) for s in subs if s.id not in linked and await repo.setting(f'subscription_removed:{s.id}')!='1']
    offset=min(max(0,offset),max(0,(len(entries)-1)//5*5));rows=[];options={};stamp=now()
    await repo.tariffs()
    for sub,key in entries[offset:offset+5]:
        label=await service.subscription_list_label(sub,key) if sub else key.name
        target=f'keydetail:{key.id}:{offset}' if key and (not sub or sub.kind=='admin') else f'sub:{sub.id}'
        expiry = sub.expires_at if sub else key.expires_at
        expired = utc(expiry) <= stamp
        inactive = (key and key.status != 'active') or (sub and await repo.setting(f'subscription_status:{sub.id}', 'active') != 'active')
        status = 'Истекла' if expired else 'Неактивна' if inactive else time_remaining(expiry)
        rows.append([(f'{label[:48]} · {status}', target)])
        if expired or inactive:
            options[target] = {'style': 'danger'}
    if offset:rows.append([('← Предыдущие',f'subscriptions:{offset-5}')])
    if offset+5<len(entries):rows.append([('Следующие →',f'subscriptions:{offset+5}')])
    return Screen('🔑 <b>Мои ключи</b>' if entries else 'У вас пока нет подписок и ключей.',[[('🛒 Купить подписку','buy')]]+rows+[back()],button_options=options)
