from app.services.device_reserve import technical
"""Administrator device navigation and confirmed, actor-bound HWID removal."""
import json,secrets,re
from html import escape
from sqlalchemy import select
from app.database.models import AppSetting,now
from app.services.screens import Screen
from app.services.vless_devices import tag,button_label,identity
from app.services.device_info import name,fields
from app.services import device_inventory

async def render(service,p,user,origin=''):
    service.service.admin(user)
    op=p[0];confirmation=None
    if op=='device_apply':
        if not re.fullmatch(r'[0-9a-f]{20}',p[1]):raise ValueError('Подтверждение недоступно.')
        row=await service.session.scalar(select(AppSetting).where(AppSetting.key=='admin_device_confirm:'+p[1]).with_for_update().execution_options(populate_existing=True))
        confirmation=json.loads(row.value) if row else {}
        if confirmation.get('actor')!=user.telegram_id or confirmation.get('done') or confirmation.get('expires',0)<now().timestamp():raise ValueError('Подтверждение устарело или уже использовано.')
        c=await service.credential(confirmation['cid']);offset=confirmation['offset']
    else:
        c=await service.credential(p[1])
        pos=3 if op in ('device','device_confirm') else 2
        offset=max(0,int(p[pos])) if len(p)>pos else 0
    panel=await service.service.server(c.server_id);api=service.service.api(panel)
    back=f'admin:r:devices:{c.id}:{offset}'
    try:items=await device_inventory.devices(api,c.remote_user_uuid,refresh=op in ('device_confirm','device_apply') or p[-1]=='refresh')
    except Exception:
        return Screen('📱 <b>Устройства</b>\n\nПанель не ответила вовремя. Количество устройств не удалось проверить.',[[('🔄 Повторить',back+':refresh')],[('♻️ Сбросить привязки',f'admin:r:confirm:devices:{c.id}')],[('← Ключ',f'admin:r:key:{c.id}')]],structured=True)
    items=sorted([d for d in items if isinstance(d,dict)],key=lambda d:(str(d.get('createdAt') or ''),tag(d)))
    full_admin=service.settings.is_owner(user.telegram_id)
    display_limit=technical(c.device_limit) if full_admin else c.device_limit
    if not full_admin and c.device_limit>0:items=items[:c.device_limit]
    offset=min(offset,max(0,(len(items)-1)//5*5));back=f'admin:r:devices:{c.id}:{offset}'
    if op in ('device','device_confirm','device_apply'):
        chosen=next((d for d in items if identity(d)==confirmation['hwid']),None) if confirmation else next((d for d in items if tag(d)==p[2]),None)
        if op=='device_apply':
            if chosen:
                from app.services.remnawave import RemnawaveApiError
                try:await api.delete_device(confirmation['hwid'],c.remote_user_uuid)
                except RemnawaveApiError as e:raise ValueError('Панель не подтвердила отвязку. Остальные устройства сохранены.') from e
            from app.services.vless_hwid import confirmed_removal
            await confirmed_removal(service.repo,c,confirmation['hwid'])
            confirmation['done']=True
            await service.repo.set_setting('admin_device_confirm:'+p[1],json.dumps(confirmation))
            if chosen:
                await service.audit(user,'device_unlink',c.id)
            screen=await render(service,['devices',str(c.id),str(offset)],user,origin)
            screen.text=('✅ Привязка устройства снята.' if chosen else 'Устройство уже было отвязано.')+'\n\n'+screen.text
            return screen
        if not chosen:return Screen('Устройство больше не найдено.',[[('← Устройства',back)]],structured=True)
        if op=='device_confirm':
            token=secrets.token_hex(10)
            await service.repo.set_setting('admin_device_confirm:'+token,json.dumps(dict(actor=user.telegram_id,cid=c.id,hwid=identity(chosen),offset=offset,expires=now().timestamp()+300,done=False)))
            target='admin:r:device_apply:'+token
            text='Отвязать <b>'+escape(name(chosen))+'</b>?\n\nБудет снята только его привязка. Остальные устройства и срок подписки сохранятся. Если у устройства осталась ссылка, оно сможет подключиться снова при наличии свободного места.'
            return Screen(text,[[('🗑 Да, отвязать',target)],[('← Отмена',f'admin:r:device:{c.id}:{tag(chosen)}:{offset}')]],button_options={target:{'style':'danger'}},structured=True)
        text='📱 <b>'+escape(name(chosen))+'</b>\n\n'+'\n\n'.join('<b>'+title+':</b> '+escape(value) for title,value in fields(chosen))
        target=f'admin:r:device_confirm:{c.id}:{tag(chosen)}:{offset}'
        return Screen(text,[[('🗑 Отвязать это устройство',target)],[('← Устройства',back)]],button_options={target:{'style':'danger'}},structured=True)
    rows=[[(button_label(d,offset+i+1),f'admin:r:device:{c.id}:{tag(d)}:{offset}')] for i,d in enumerate(items[offset:offset+5])]
    if offset:rows.append([('← Предыдущие',f'admin:r:devices:{c.id}:{offset-5}')])
    if offset+5<len(items):rows.append([('Следующие →',f'admin:r:devices:{c.id}:{offset+5}')])
    rows.extend([[('🔄 Обновить',f'admin:r:devices:{c.id}:{offset}:refresh')],[('♻️ Сбросить все устройства',f'admin:r:confirm:devices:{c.id}')],[('Лимит устройств',f'admin:r:input:limit:{c.id}')],[('← Ключ',f'admin:r:key:{c.id}')]])
    text=f'📱 <b>Устройства · {len(items)} / {display_limit}</b>\n\n'+('Выберите устройство, чтобы посмотреть сведения или отвязать его.' if items else 'Устройства ещё не подключались.')
    if full_admin:text+=f'\n\nТариф: {c.device_limit} · резерв: 1 · полный лимит: {display_limit}'
    text+='\n\n<b>Что показывает список:</b> зарегистрированные в панели HWID. Отклонённые попытки сверх лимита этот API не передаёт; отсутствие второго устройства не означает отсутствие попытки.'
    if c.device_limit>0 and len(items)>=display_limit:
        text+='\n\n⚠️ Свободных мест нет. Для нового устройства отвяжите старое или сбросьте все привязки. Лимит и оплаченный срок сохранятся.'
    return Screen(text,rows,structured=True)
