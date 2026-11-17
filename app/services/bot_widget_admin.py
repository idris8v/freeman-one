"""Administrator-only widget issuance using the existing committed grant tokens."""
import json
from sqlalchemy import select
from app.database.models import User,now
from app.database.repositories import SQLRepository
from app.services.bot_widget_checkout import key


def require(settings,user,root):
    if not settings.is_owner(user.telegram_id) or user.status=='blocked' or root.get('admin_mode') is not True or root.get('actor')!=user.id:
        raise PermissionError('Администраторская выдача недоступна.')


async def prepare(repo,settings,user,token,root,tid,anti,mode):
    require(settings,user,root)
    if mode not in ('sale','free') or anti not in (0,1):raise ValueError('Неверный режим выдачи.')
    previous=root.get('admin_issue')
    selection=[tid,anti,mode]
    if previous:
        if previous['selection']!=selection:raise ValueError('Выдача уже начата с другими условиями. Завершите её или откройте новую карточку.')
        return {'_admin_grant':previous['token']},None
    shown=next((p for p in root['plans'] if len(p)>=4 and p[3]==tid),None)
    if not shown:raise ValueError('Тариф недоступен.')
    if root.get('client_id') is not None:
        if anti and root.get('antig') is not True:
            raise ValueError('Антиглушилка недоступна в этой карточке.')
        from app.services.admin_client_grant import ClientGrant
        grants=ClientGrant(repo,settings)
        screen=await grants.preview(root['client_id'],user,tariff_id=tid,card_tariff=True,antig=bool(anti))
        target=next(target for row in screen.rows for _,target in row if target.startswith('admin:clientgrant:apply:'))
        grant_token=target.rsplit(':',1)[1]
        draft=json.loads(await repo.setting('client_grant:'+grant_token))
        expected=shown[2]+(shown[4] if anti else 0)
        if [draft['devices'],draft['months']]!=shown[:2] or draft['tariff_sale']!=expected:
            raise ValueError('Тариф или цена изменились. Откройте новую карточку.')
        draft['widget_client']=dict(root=key(token),selection=selection)
        await repo.set_setting('client_grant:'+grant_token,json.dumps(draft))
        root['admin_issue']=dict(selection=selection,token=grant_token)
        await repo.set_setting(key(token),json.dumps(root))
        return {'_admin_grant':grant_token},None
    from app.services.admin_subscription import preview
    from app.services.bot_antig_price import tariff_total
    from app.services.bot_antig_test import resolve
    target=await preview(repo,user,f'id{tid}')
    grant_token=target.rsplit(':',1)[1]
    data=json.loads(await repo.setting('admin_grant:'+grant_token))
    terms=data['sale_terms']
    tariff=await repo.tariff_by_id(tid)
    extra=await tariff_total(repo,tariff) if anti else 0
    if [terms['devices'],terms['months']]!=shown[:2] or terms['total']+extra!=shown[2]+((shown[4] if len(shown)>4 else root['extra']*shown[1]) if anti else 0):
        raise ValueError('Тариф или цена изменились. Откройте новую карточку перед выдачей.')
    if terms['total']+extra>100000000:raise ValueError('Итоговая цена слишком велика.')
    if anti:
        terms['bot_test_access']=await resolve(repo,settings,True)
        data['antig_test']=True
        terms['antig_base_total']=terms['total'];terms['antig_surcharge']=extra
        terms['total']+=extra;terms['base_price']+=extra
    # Stored server-side only. Never accept grant tokens or accounting modes
    # from the widget's response. The first request freezes one grant per root.
    data['widget_admin']=dict(root=key(token),selection=selection)
    data['widget_accounting']=mode
    await repo.set_setting('admin_grant:'+grant_token,json.dumps(data))
    root['admin_issue']=dict(selection=selection,token=grant_token)
    await repo.set_setting(key(token),json.dumps(root))
    return {'_admin_grant':grant_token},None


async def execute(database,settings,uid,grant_token):
    # The preparation transaction must commit before remote provisioning. An
    # uncertain result retries the same persisted admin-grant:<token> operation.
    async with database.sessions() as session:
        async with session.begin():
            repo=SQLRepository(session)
            role=await repo.setting(f'staff_role:{uid}')
            blocked=await repo.setting(f'staff_blocked:{uid}')=='1'
            settings.set_staff_role(uid,None if blocked else role)
            user=await session.scalar(select(User).where(User.telegram_id==uid))
            if not user or user.status=='blocked' or not settings.is_owner(uid):raise PermissionError('Администраторская выдача недоступна.')
            from app.services.offers import lock_customer
            await lock_customer(repo,user.id)
            client=json.loads(await repo.setting('client_grant:'+grant_token,'{}'))
            if client.get('widget_client'):
                meta=client['widget_client']
                root=json.loads(await repo.setting(meta['root'],'{}'))
                require(settings,user,root)
                if (root.get('client_id')!=client['user'] or client['actor']!=uid
                        or root.get('admin_issue')!=dict(token=grant_token,selection=meta['selection'])):
                    raise PermissionError('Получатель или условия выдачи изменились.')
                from app.services.admin_client_grant import ClientGrant
                return await ClientGrant(repo,settings).apply(grant_token,user,complimentary=meta['selection'][2]=='free')
            data=json.loads(await repo.setting('admin_grant:'+grant_token,'{}'))
            meta=data.get('widget_admin',{})
            root=json.loads(await repo.setting(meta.get('root',''),'{}'))
            require(settings,user,root)
            binding=root.get('admin_issue',{})
            if data.get('actor')!=uid or binding.get('token')!=grant_token or binding.get('selection')!=meta.get('selection') or data.get('widget_accounting') not in ('sale','free'):
                raise PermissionError('Условия выдачи не подтверждены.')
            from app.services.screens import ScreenService
            from app.services.integrations import MockPaymentService,MockProvisioningService
            service=ScreenService(repo,settings,MockPaymentService(),MockProvisioningService())
            suffix=':free' if data['widget_accounting']=='free' else ''
            return await service.render(f'admin_free:{data["months"]}:0:{grant_token}'+suffix,user)


async def custom(service,user,root,anti):
    require(service.settings,user,root)
    if root.get('admin_issue'):raise ValueError('Выдача уже начата. Откройте новую карточку для других параметров.')
    client_id=root.get('client_id')
    screen=await service.render(f'admin:grantconfig:new:{client_id}:card' if client_id else f'admin:grantconfig:new:{user.id}:buy',user)
    if anti:
        if client_id:
            from app.services.bot_antig_price import require_enabled
            await require_enabled(service.repo)
        from app.services.bot_antig_test import resolve
        token=screen.admin_input_action.split(':')[3]
        data=json.loads(await service.repo.setting('bot_grant_draft:'+token))
        data['widget_access']=await resolve(service.repo,service.settings,True)
        data['widget_prefill']=True
        await service.repo.set_setting('bot_grant_draft:'+token,json.dumps(data))
        screen.text+='\n\n🛡 Антиглушилка включена для этой выдачи. Укажите итоговую цену целиком; без цены — бесплатно.'
        from app.services.admin_client_grant import rich
        updated=rich(screen.text,screen.rows);updated.admin_input_action=screen.admin_input_action
        screen=updated
    return {},screen


async def review(repo,settings,user,token,root,tid,anti):
    require(settings,user,root)
    if root.get('admin_issue'):raise ValueError('Выдача уже начата. Завершите её перед новым оформлением.')
    result,_=await prepare(repo,settings,user,token,root,tid,anti,'sale')
    root['admin_review']=root.pop('admin_issue')
    root['stage']=1
    await repo.set_setting(key(token),json.dumps(root))
    grant=result['_admin_grant']
    if root.get('client_id'):
        draft=json.loads(await repo.setting('client_grant:'+grant))
        terms=dict(devices=draft['devices'],months=draft['months'],total=draft['tariff_sale'])
    else:
        data=json.loads(await repo.setting('admin_grant:'+grant))
        terms=data['sale_terms']
    from app.services.bot_admin_checkout import today_summary
    from sqlalchemy.exc import SQLAlchemyError
    revenue=sales=None
    try:revenue,sales=await today_summary(repo)
    except SQLAlchemyError:pass
    return dict(review=dict(devices=terms['devices'],months=terms['months'],total=terms['total'],antig=bool(anti),
        revenue=revenue,sales=sales,after=revenue+terms['total'] if revenue is not None else None)),None


async def confirm(repo,settings,user,token,root,tid,anti):
    require(settings,user,root)
    saved=root.get('admin_review',{})
    if root.get('stage')!=1 or saved.get('selection')!=[tid,anti,'sale']:
        raise ValueError('Сначала проверьте выбранный тариф и сводку продажи.')
    if root.get('admin_issue') not in (None,saved):raise ValueError('Выдача уже начата с другими условиями.')
    root['admin_issue']=saved
    await repo.set_setting(key(token),json.dumps(root))
    return {'_admin_grant':saved['token']},None
