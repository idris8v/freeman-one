"""Opaque chat commands and explicit confirmations, shared by web and Telegram."""
import json
import secrets
import time
from sqlalchemy import select
from app.database.models import AIConversation,AIMessage
from app.services.freeman_ai.security import fingerprint
from .actions import ActionService,ActionDenied
from .persistence import DurableRepository,PanelAdapter
from .presentation import copy_action

async def command(backend,key,kind,label,**params):
    sessions=backend.s.info.get('agent_sessions')
    if sessions is None:raise ValueError('durable_journal_unconfigured')
    token=secrets.token_urlsafe(24)
    ttl=86400
    if kind in ('copy','import','qr','renew'):
        credential=await backend.resolve(f'key:{key}')
        params['key_fingerprint']=fingerprint(backend.settings,credential.subscription_url)
        ttl=900
    await DurableRepository(sessions,backend.settings).put(token,dict(subject=str(backend.conv.id),
        key=key,kind=kind,params=params,status='command',expires=time.time()+ttl))
    return dict(type='agent_action',token=token,label=label,decision='open')

async def key_actions(backend,c):
    from .rollout import mutations_enabled
    actions=[await command(backend,c.id,'devices','Подключенные устройства'),
             await command(backend,c.id,'qr','QR подписки'),
             await command(backend,c.id,'copy','Скопировать ключ'),
             await command(backend,c.id,'import','Добавить в INCY')]
    if await mutations_enabled(backend.s):
        actions.append(await command(backend,c.id,'reissue','Перевыпустить ключ'))
    return actions

async def device_actions(backend,c,rows):
    from .rollout import mutations_enabled
    if not await mutations_enabled(backend.s):
        return []
    actions=[]
    for ref,d in rows:
        actions.append(await command(backend,c.id,'detach','Отвязать '+(d.get('deviceModel') or 'устройство')[:50],device_refs=[ref]))
    if len(rows)>1:actions.append(await command(backend,c.id,'detach','Отвязать все устройства',device_refs=[ref for ref,_ in rows]))
    return actions

async def handle(sessions,settings,token,cid,decision='open'):
    from .backend import Backend
    repo=DurableRepository(sessions,settings);row=await repo.get(token)
    if not row or row['subject']!=str(cid) or row['expires']<time.time() and row['status'] in ('prepared','command'):
        raise ActionDenied('action_unavailable')
    if row['kind'] in ('reissue','detach') and decision!='cancel':
        from .rollout import mutations_enabled
        async with sessions() as session:
            if not await mutations_enabled(session):
                return dict(text='Изменение из чата пока недоступно. Отвязать устройство можно в разделе «Мои ключи». С перевыпуском поможет оператор.',actions=[])
    panel=PanelAdapter(sessions,settings,cid)
    if row['status']!='command':
        service=ActionService(repo,panel)
        if decision=='cancel':
            await service.cancel(token,str(cid));return dict(text='Хорошо, ничего не меняем 🙂',actions=[])
        if decision!='confirm':
            # Opening a confirmation is read-only even if request is replayed.
            return confirmation(token,row)
        result=await service.confirm(token,str(cid))
        if result['status']!='done':
            return dict(status='uncertain',text='⏳ Результат операции пока не подтверждён. Проверю состояние, не выполняя действие повторно.',actions=[dict(type='agent_action',token=token,label='Проверить результат',decision='confirm')])
        actions=[]
        async with sessions.begin() as s:
            conv=await s.get(AIConversation,cid);backend=Backend(s,settings,conv,[]);s.info['agent_sessions']=sessions
            c=await backend.resolve('key:'+str(row['key']))
            copy=await command(backend,c.id,'copy','Скопировать новый ключ')
            a=copy_action(result['snapshot']['link'],settings.web_base_url.rstrip('/')+'/support/action#'+copy['token']);a['label']='Скопировать новый ключ' if row['kind']=='reissue' else 'Скопировать ключ';actions.append(a)
            actions+=await key_actions(backend,c)
            if c.access_key_id and conv.user_id==c.user_id:
                from .registry import cabinet_key_url
                actions.append(dict(type='link',label='Управление устройствами',url=cabinet_key_url(c.access_key_id)))
        return dict(text='✅ Новый ключ готов. Срок и лимит подписки сохранились.' if row['kind']=='reissue' else '✅ Выбранные устройства отвязаны.',actions=actions)
    if decision!='open':raise ActionDenied('invalid_command_decision')
    async with sessions.begin() as s:
        conv=await s.get(AIConversation,cid)
        if not conv or conv.mode!='ai':raise ActionDenied('conversation_not_automated')
        if row['kind']=='diagnostic_scope':
            from app.services.customer_identity import lock_nonce
            from app.database.repositories import SQLRepository
            from .state import StateStore
            from .support_content import SCOPE_OPTIONS, scope_followup
            await lock_nonce(s,'diagnostic-scope:'+str(cid))
            await s.refresh(conv)
            if conv.mode!='ai' or row['params']['generation']!=conv.generation:
                raise ActionDenied('diagnostic_choice_expired')
            store=StateStore(s,settings)
            state=await store.load(conv)
            choice=row['params']['choice']
            choice_key=f'agent_v21_scope:{cid}:{conv.generation}'
            repo=SQLRepository(s)
            previous=await repo.setting(choice_key,None)
            if previous is not None and previous!=str(choice):
                raise ActionDenied('diagnostic_choice_already_selected')
            if previous is None:
                await repo.set_setting(choice_key,str(choice))
                s.add(AIMessage(conversation_id=cid,role='client',text=SCOPE_OPTIONS[choice],
                    generation=conv.generation,actions='[]',source_hash=fingerprint(settings,choice_key)))
            recent = (await s.scalars(
                select(AIMessage.text).where(AIMessage.conversation_id == cid, AIMessage.role == 'client')
                .order_by(AIMessage.id.desc()).limit(4)
            )).all()
            text = scope_followup(state, choice, [value for value in recent if value not in SCOPE_OPTIONS])
            state.generation=conv.generation
            await store.save(conv,state,conv.generation)
            return dict(text=text,actions=[])
        if row['kind']=='region':
            from .support_content import region_reply
            text,actions=region_reply()
            return dict(text=text,actions=actions)
        history=list(await s.scalars(select(AIMessage).where(AIMessage.conversation_id==cid,AIMessage.role=='client').order_by(AIMessage.id.desc()).limit(100)))
        backend=Backend(s,settings,conv,history);s.info['agent_sessions']=sessions
        c=await backend.resolve('key:'+str(row['key']))
        verify_read_capability(settings,row,c.subscription_url)
        if row['kind']=='key':
            from app.database.models import AccessKey,Subscription
            from app.services.subscription_labels import current
            from app.services.vless_hwid import cache_state

            sub=await s.get(Subscription,c.subscription_id)
            access_key=await s.get(AccessKey,c.access_key_id) if c.access_key_id else None
            name,term=await current(backend.repo,sub,access_key)
            count,_=await cache_state(s,sub.id)
            number=c.access_key_id or c.id
            registered=f'{count} из {sub.device_limit or "∞"}' if count is not None else 'откройте список для проверки'
            details=(
                f'🔑 **Подписка №{number}**\n\n'
                f'{name}\n'
                f'Тариф: {term or "индивидуальный"}\n'
                f'Дата выдачи: {sub.created_at:%d.%m.%Y}\n'
                f'Действует до: {sub.expires_at:%d.%m.%Y}\n'
                f'Подключённые устройства по последней проверке: {registered}'
            )
            from .state import StateStore
            state_store=StateStore(s,settings)
            state=await state_store.load(conv)
            state.selected_key=c.id
            state.generation=conv.generation
            await state_store.save(conv,state,conv.generation)
            actions=await key_actions(backend,c)
            renew=await backend.purchase({'renewal':True,'key_ref':f'key:{c.id}'},state)
            antig=await backend.antig({'key_ref':f'key:{c.id}'},state)
            return dict(text=details,actions=actions+renew['actions']+antig.get('actions',[]))
        if row['kind']=='devices':
            result=await backend.devices(dict(key_ref='key:'+str(c.id)),None)
            names=[
                f"• {d['name']} · {d.get('os') or '—'} · "
                f"{(d.get('application') or 'приложение не указано')[:80]} · "
                f"{(d.get('registered_at') or 'дата не указана')[:10]}"
                for d in result['devices']
            ]
            return dict(text='📱 Подключённые устройства\n\n'+ ('\n'.join(names) if names else 'Подключённых устройств пока нет.'),actions=result.get('actions',[]))
        if row['kind'] in ('detach','reissue'):
            devices=row['params'].get('device_refs',[])
            prepared=await backend.propose(c,row['kind'],devices)
            if not prepared.get('operation'):return dict(text=prepared['guidance'],actions=[])
            action_token=prepared['operation'];prepared_row=await repo.get(action_token)
            return confirmation(action_token,prepared_row)
        if row['kind']=='copy':
            return dict(text='🔑 Это ваш ключ подписки. Скопируйте его целиком.',copy_value=c.subscription_url,
                actions=[copy_action(c.subscription_url,settings.web_base_url.rstrip('/')+'/support/action#'+token)])
        if row['kind']=='qr':
            from .presentation import verified_qr
            # Fail closed if independent decoder is unavailable.
            verified_qr(c.subscription_url)
            return dict(text='🔑 Отсканируйте QR-код на том устройстве, куда добавляете подписку.',actions=[dict(type='agent_image',label='QR подписки',token=token)])
        if row['kind']=='import':
            return dict(text='📱 Откройте страницу и нажмите «Добавить в INCY». Если приложение не откроется, на странице есть полный ключ для ручного импорта.',actions=[dict(type='link',label='Добавить в INCY',url=settings.web_base_url.rstrip('/')+'/support/connect#'+token)])
        raise ActionDenied('unsupported_command')

def confirmation(token,row):
    if row['kind']=='reissue':
        text='🔑 Перевыпустить ключ?\n\nСтарая ссылка перестанет работать на всех устройствах. Тариф, срок и лимит останутся прежними.';label='Перевыпустить ключ'
    else:
        count=len(row['devices']);names=row['before'].get('device_names',{});name=names.get(row['devices'][0],'выбранное устройство') if count==1 else str(count)+' устройства'
        text='📱 Отвязать «'+name+'»?\n\nОстальные подключения сохранятся.';label='Подтвердить отвязку'
    return dict(text=text,actions=[dict(type='agent_action',token=token,decision='confirm',label=label),dict(type='agent_action',token=token,decision='cancel',label='Отмена')])

def verify_read_capability(settings, row, link):
    from hmac import compare_digest

    if row['kind'] not in ('copy','import','qr','renew'):
        return
    expected=row.get('params',{}).get('key_fingerprint','')
    if row['expires']<=time.time() or not compare_digest(expected,fingerprint(settings,link)):
        raise ActionDenied('key_link_changed')
