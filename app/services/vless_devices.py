"""Owner-scoped HWID device navigation and confirmed single-device removal."""
import hashlib,json,secrets
from datetime import datetime,timezone
from html import escape
from sqlalchemy import select,update
from app.database.models import VlessCredential,User,now
from app.services.screens import Screen
from app.services.remnawave import RemnawaveService,RemnawaveApiError

def identity(device):return str(device.get('hwid') or device.get('uuid') or '')
def tag(device):return hashlib.sha256(identity(device).encode()).hexdigest()[:20]
from app.services.device_info import name as label,fields, date
def button_label(device, number):
    platform=(str(device.get('platform') or '')+' '+label(device)+' '+str(device.get('userAgent') or '')).casefold()
    if any(word in platform for word in ('android','pixel','samsung')):icon='🤖'
    elif any(word in platform for word in ('iphone','ipad','ios')):icon='🍎'
    elif any(word in platform for word in ('windows','macos','mac os','macbook','linux','desktop','computer')):icon='💻'
    else:icon='📱'
    try:connected=datetime.fromisoformat(str(device.get('createdAt')).replace('Z','+00:00')).astimezone(timezone.utc).strftime('%d.%m.%Y')
    except (ValueError,TypeError):connected='дата неизвестна'
    prefix=f'{number}. {icon} '
    suffix=' · '+connected
    return prefix+label(device)[:max(1,64-len(prefix)-len(suffix))]+suffix

async def render(repo,settings,user,action):
    p=action.split(':');op=p[1]
    confirmation=None
    if op=='apply':
        await repo.session.execute(update(User).where(User.id==user.id).values(status=User.status))
        confirmation=json.loads(await repo.setting('vless_device_confirm:'+p[2],'{}'))
        if confirmation.get('actor')!=user.id or confirmation.get('done') or confirmation.get('expires',0)<now().timestamp():raise ValueError('Подтверждение устарело или уже использовано.')
        sid=confirmation['sid']
    else:sid=int(p[2])
    sub=await repo.subscription(user.id,sid)
    c=await repo.session.scalar(select(VlessCredential).where(VlessCredential.subscription_id==sid,VlessCredential.user_id==user.id)) if sub else None
    if not c or c.status=='deleted' or await repo.setting(f'subscription_removed:{sid}')=='1':raise ValueError('Подписка недоступна.')
    service=RemnawaveService(repo,settings);panel=await service.server(c.server_id);api=service.api(panel)
    from app.services.device_inventory import devices
    try:items=await devices(api,c.remote_user_uuid,refresh=op in ('confirm','apply') or p[-1]=='refresh')
    except Exception:
        return Screen('📱 <b>Устройства</b>\n\nПанель не ответила вовремя. Повторите запрос.',[[('🔄 Повторить',f'vd:list:{sid}:0:refresh')],[('← Ключ',f'sub:{sid}')]],structured=True)
    items=sorted([d for d in items if isinstance(d,dict)],key=lambda d:(str(d.get('createdAt') or ''),identity(d)))
    visible_limit=c.device_limit
    if c.device_limit>0:items=items[:c.device_limit]
    if op=='list':
        offset=max(0,int(p[3])) if len(p)>3 else 0;offset=min(offset,max(0,(len(items)-1)//5*5))
        noun='устройстве' if visible_limit==1 else 'устройствах'
        text=f'📱 <b>Подключённые устройства</b>\n\nДанный ключ можно использовать только на {visible_limit} {noun}.\n\nПодключено: <b>{len(items)} из {visible_limit}</b>.'
        if visible_limit and len(items)>visible_limit:text+='\n\n🔴 <b>Лимит превышен.</b> Выберите лишнее устройство и снимите его привязку.'
        text+='\n\nНажмите на устройство, чтобы посмотреть сведения и отключить его. Новые устройства находятся внизу списка.' if items else '\n\nУстройства ещё не подключались.'
        rows=[[(button_label(d,offset+i+1),f'vd:view:{sid}:{tag(d)}')] for i,d in enumerate(items[offset:offset+5]) if identity(d)]
        nav=[]
        if offset:nav.append(('← Предыдущие',f'vd:list:{sid}:{max(0,offset-5)}'))
        if offset+5<len(items):nav.append(('Следующие →',f'vd:list:{sid}:{offset+5}'))
        if nav:rows.append(nav)
        reset=f'device_rotate:{sid}'
        return Screen(text,rows+[[('🔄 Обновить',f'vd:list:{sid}:{offset}:refresh')],[('Отвязать все устройства',reset)],[('← Назад',f'sub:{sid}')]],button_options={reset:{'style':'danger'}},structured=True)
    chosen=next((d for d in items if (identity(d)==confirmation['hwid'] if confirmation else tag(d)==p[3])),None)
    if not chosen:return Screen('Устройство уже отключено или список изменился.',[[('📱 Устройства',f'devices:{sid}')]])
    if op=='view':
        text=f'📱 <b>{escape(label(chosen))}</b>\n\n'+ '\n\n'.join(f'<b>{title}:</b> {escape(str(value or "Нет данных"))}' for title,value in fields(chosen))
        target=f'vd:confirm:{sid}:{tag(chosen)}'
        return Screen(text,[[('Отключить устройство',target)],[('← Устройства',f'devices:{sid}')]],button_options={target:{'style':'danger'}},structured=True)
    if op=='confirm':
        token=secrets.token_hex(12)
        await repo.set_setting('vless_device_confirm:'+token,json.dumps({'actor':user.id,'sid':sid,'hwid':identity(chosen),'expires':now().timestamp()+300}))
        target='vd:apply:'+token
        return Screen(f'Отключить <b>{escape(label(chosen))}</b>?\n\nБудет снята только его привязка. Остальные устройства и срок подписки сохранятся. Устройство сможет зарегистрироваться снова, если у него осталась ссылка подписки.',[[('Да, отключить',target)],[('Отмена',f'vd:view:{sid}:{tag(chosen)}')]],button_options={target:{'style':'danger'}},structured=True)
    if op=='apply':
        try:await api.delete_device(identity(chosen),c.remote_user_uuid)
        except RemnawaveApiError as e:raise ValueError('Панель не подтвердила отключение устройства.') from e
        from app.services.vless_hwid import confirmed_removal
        await confirmed_removal(repo,c,identity(chosen))
        confirmation['done']=True;await repo.set_setting('vless_device_confirm:'+p[2],json.dumps(confirmation))
        return Screen('✅ Привязка устройства снята. Остальные устройства сохранены.',[[('📱 Устройства',f'devices:{sid}')],[('← Ключ',f'sub:{sid}')]])
    raise ValueError('Действие недоступно.')
