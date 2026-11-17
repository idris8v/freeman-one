"""Device-first bot catalogue; payment callbacks keep the existing tariff IDs."""
from html import escape
from aiogram.types import InputRichMessage
from app.services.screens import Screen, money, duration
from app.services.pricing import quote

SIZE=5
DEVICE_COLUMNS=3

async def attach_media(repo,screen):
    """Keep configured buy-screen media and intro together with the native table."""
    from aiogram.types import BufferedInputFile,InputMediaPhoto,InputMediaVideo
    from app.bot.rich_copy import text_blocks
    blocks=[]
    media=[screen.banner] if screen.banner else []
    if screen.photo:media.append(('photo',screen.photo))
    elif screen.animation:media.append(('animation',screen.animation))
    elif screen.video:media.append(('video',screen.video))
    for kind,value in media:
        if isinstance(value,tuple):value=BufferedInputFile(value[0],filename=value[1])
        kind='photo' if kind=='photo' else 'video'
        blocks.append(dict(type=kind,**{kind:(InputMediaPhoto if kind=='photo' else InputMediaVideo)(media=value)}))
    intro=await repo.setting('intro:buy','')
    if intro:blocks.extend(text_blocks(escape(intro)+'\n\n'))
    if blocks:screen.rich=InputRichMessage(blocks=[*blocks,*screen.rich.blocks],skip_entity_detection=True)
    return screen

def devices_label(n):
    return f'{n} '+('устройство' if n%10==1 and n%100!=11 else 'устройства' if n%10 in (2,3,4) and n%100 not in (12,13,14) else 'устройств')

def catalog_term(months):
    return '1 год' if months == 12 else duration(months)


def rich_table(title,intro,headers,rows,note=''):
    def cell(value,header=False):
        return dict(text=value if isinstance(value,list) else str(value),is_header=header,align='left',valign='middle')
    blocks=[dict(type='heading',text=title,size=1),dict(type='paragraph',text=intro+'\n\n')]
    if rows:
        blocks.append(dict(type='table',cells=[[cell(v,True) for v in headers],*[[cell(v) for v in row] for row in rows]],is_bordered=True,is_striped=True,is_compact=True))
    if note:blocks.append(dict(type='paragraph',text=note))
    return InputRichMessage(blocks=blocks,skip_entity_detection=True)

def price_text(base,total,*,rich=False,html=False):
    current=money(total)
    if not 0<=total<base:return current
    saved=base-total;percent=saved*100//base
    benefit=(f'−{percent}%' if percent else 'Скидка')+f' · выгода {money(saved)}'
    if rich:return [dict(type='strikethrough',text=money(base)),f' → {current}\n{benefit}']
    if html:return f'<s>{money(base)}</s> → <b>{current}</b>\n{benefit}'
    return f'{money(base)} → {current} ({benefit})'

async def available(repo):
    result=[]
    for t in await repo.tariffs():
        if await repo.tariff_setting(t,'tariff_enabled','1')=='1' and await repo.tariff_setting(t,'tariff_deleted','0')!='1':result.append(t)
    return result

def page_offset(offset,total,size=SIZE):
    return min(max(0,offset),max(0,(total-1)//size*size))

async def render(service,action,user):
    repo,settings=service.repo,service.settings
    p=action.split(':');mode=p[0];sid=0;selected=0;offset=0;column=0
    try:
        if mode=='renew':sid=int(p[1])
        elif mode=='buy_devices':selected=int(p[1]);offset=int(p[2]) if len(p)>2 else 0
        elif mode=='renew_devices':sid=int(p[1]);selected=int(p[2]);offset=int(p[3]) if len(p)>3 else 0
        elif mode=='catalog_groups':sid=int(p[1]);offset=int(p[2])
        elif mode=='catalog_table':sid=int(p[1]);offset=int(p[2]) if len(p)>2 else 0;column=int(p[3]) if len(p)>3 else 0
    except (ValueError,IndexError):raise ValueError('Выбор тарифа устарел. Откройте покупку заново.')
    if sid<0 or selected<0:raise ValueError('Некорректный выбор тарифа.')
    sub=await repo.subscription(user.id,sid) if sid else None
    if sid and not sub:return service.missing()
    if sid and settings.is_owner(user.telegram_id):
        from app.services.admin import AdminService
        target=f'admin:renewcustom:periods:{sid}:{selected}:{offset}' if selected else f'admin:renewcustom:devices:{sid}:{offset}'
        return await AdminService(repo,settings).render(target,user)
    if mode=='buy':await repo.track_funnel(user.id,'plans')
    tariffs=await available(repo)
    if mode == 'renew' and len(p) == 3 and p[2] == 'quick' and sub.tariff_id:
        tariff = next((t for t in tariffs if t.id == sub.tariff_id and t.device_limit == sub.device_limit), None)
        if tariff:
            from app.services.admin_client_grant import rich
            from app.services.billing import extend
            from app.services.subscription_labels import expiry_date
            from app.services.bot_antig_price import enabled as antig_enabled, tariff_total
            from app.services.bot_antig_addon import rate_remaining
            from app.database.models import now

            price = (await quote(repo, user.id, tariff, sid))[0]
            new_expiry = expiry_date(extend(sub.expires_at, tariff.months))
            rows = [[(f'Продлить VPN · {money(price)}', f'tariff:id{tariff.id}:{sid}')]]
            if await antig_enabled(repo):
                total = price + await tariff_total(repo, tariff) + await rate_remaining(repo, sub, now())
                rows.append([(f'С антиглушилкой · {money(total)}', f'ag:quote:{sid}:{tariff.id}')])
            price_label = 'Стоимость VPN'
            text = (f'🔄 <b>Продлить текущий ключ</b>\n\n{devices_label(tariff.device_limit)} · {duration(tariff.months)}'
                    f'\n{price_label}: <b>{money(price)}</b>\nНовая дата окончания: <b>{new_expiry}</b>'
                    '\n\nСсылка на ключ сохранится.')
            return rich(text, rows + [[('Изменить тариф', f'renew:{sid}')],
                               [('← Ключ', f'sub:{sid}')]])
    if sid and not selected:
        # Renewal is a wizard, not a second display of the whole price matrix.
        # Existing renew callbacks (including old messages) always restart here.
        from app.bot.rich_copy import text_blocks
        groups=sorted({t.device_limit for t in tariffs} | ({sub.device_limit} if settings.is_owner(user.telegram_id) and sub.device_limit>0 else set()))
        offset=page_offset(offset,len(groups));shown=groups[offset:offset+SIZE]
        title='🔄 <b>Продление подписки</b>'
        text=title+'\n\n<b>Шаг 1 из 2 — количество устройств</b>\n\nНа сколько устройств хотите продлить подписку? Срок выберете на следующем шаге.'
        if sub.kind=='trial':
            title='✨ <b>Оставить подключение</b>'
            text=title+'\n\nПосле оплаты сохранится тот же ключ. Устройства будут работать в пределах лимита нового тарифа.\n\nВыберите количество устройств, затем срок.'
        if not groups:text+='\n\nСейчас нет доступных тарифов.'
        rows=[[(f'📱 {devices_label(n)}',f'renew_devices:{sid}:{n}')] for n in shown]
        if offset:rows.append([('← Предыдущие устройства',f'catalog_groups:{sid}:{offset-SIZE}')])
        if offset+SIZE<len(groups):rows.append([('Следующие устройства →',f'catalog_groups:{sid}:{offset+SIZE}')])
        rows.append([('← Ключ',f'sub:{sid}')])
        return Screen(text,rows,rich=InputRichMessage(blocks=text_blocks(text),skip_entity_detection=True),structured=True,appearance_page='renew')
    prices={t.id:(await quote(repo,user.id,t,sid))[0] for t in tariffs}
    group_target=f'renew:{sid}' if sid else 'buy'
    note='📱 Ключ можно установить на телефон, компьютер, планшет или телевизор с поддержкой VPN-приложений.\n\nВыберите, сколько устройств будете использовать одновременно.'
    if selected:
        chosen=sorted((t for t in tariffs if t.device_limit==selected),key=lambda t:(t.months,t.id))
        if sid and settings.is_owner(user.telegram_id) and not chosen:
            from app.services.bot_admin_renew import BotAdminRenew
            return await BotAdminRenew(repo,settings).periods(sid,selected,user)
        if not chosen:raise ValueError('Тарифы для этого числа устройств больше недоступны. Выберите устройства заново.')
        offset=page_offset(offset,len(chosen));shown=chosen[offset:offset+SIZE]
        rows=[];table=[]
        from app.services.bot_antig_price import enabled as antig_enabled
        antig_available=await antig_enabled(repo)
        antig_choice=bool(sid and antig_available)
        purchase_addon=not sid and not settings.role(user.telegram_id) and antig_available
        if antig_choice and sub.kind=='trial':
            from app.services.bot_antig_addon import rate_remaining
            from app.database.models import now
            try:
                await rate_remaining(repo,sub,now())
            except ValueError:
                # A legacy active trial without a priced add-on can still
                # convert to paid and add it afterward.
                antig_choice=False
        for t in shown:
            price = money(prices[t.id])
            label = catalog_term(t.months)
            if sum(other.months == t.months for other in chosen) > 1:
                label += f' · №{t.id}'
            table.append([label, price_text(t.price, prices[t.id], rich=True)])
            target = f'tariff:id{t.id}'+(f':{sid}' if sid else '')
            if antig_choice:
                target = f'ag:ask:{sid}:{t.id}'
            elif purchase_addon:
                target = f'bantig:{t.id}'
            rows.append([(f'{label} — '+('бесплатно' if settings.can_free_subscription(user.telegram_id) else price+(f' · −{(t.price-prices[t.id])*100//t.price}%' if 0<=prices[t.id]<t.price else '')),target)])
        target=lambda at:f'renew_devices:{sid}:{selected}:{at}' if sid else f'buy_devices:{selected}:{at}'
        if offset:rows.append([('← Предыдущие сроки',target(offset-SIZE))])
        if offset+SIZE<len(chosen):rows.append([('Следующие сроки →',target(offset+SIZE))])
        if sid:note='Выберите срок. Оплаченный период добавится к оставшемуся сроку этого же ключа.'
        if sid and settings.is_owner(user.telegram_id):rows.append([('⚙️ Свой срок',f'admin:input:renewcustom:{sid}:{selected}')])
        rows.append([('← Изменить количество устройств',group_target)])
        title='На какой срок?';intro=f'Шаг 2 из 2 · {devices_label(selected)}.\n\nВыберите срок '+('продления.' if sid else 'подписки.')
        text='<b>'+title+'</b>\n\n'+escape(intro)+'\n\n'+'\n\n'.join(f'{term} · '+price_text(t.price,prices[t.id],html=True) for t,(term,_) in zip(shown,table))
        return Screen(text,rows,rich=rich_table(title,intro,['Срок','Цена, ₽'],table,note),structured=True,appearance_page='renew' if sid else 'buy')
    groups=sorted({t.device_limit for t in tariffs});months=sorted({t.months for t in tariffs})
    offset=page_offset(offset,len(groups));shown=groups[offset:offset+SIZE]
    title='На сколько устройств?'
    intro='Шаг 1 из 2 · Сначала выберите количество устройств, затем срок.' if tariffs else 'Сейчас нет доступных тарифов.'
    if sub:intro+=f'\n\nПродление подписки #{sid}. Сейчас: {devices_label(sub.device_limit)}.'
    # Terms are rows: all available periods remain visible on a narrow phone.
    # Extra device groups receive another compact table, never hidden term columns.
    blocks=list(rich_table(title,intro,[],[]).blocks);fallback=[]
    for index in range(0,len(shown),DEVICE_COLUMNS):
        counts=shown[index:index+DEVICE_COLUMNS]
        headers=['Срок',*[f'{n} устр.' for n in counts]];table=[];plain=[]
        for month in months:
            term = catalog_term(month)
            line=[term];plain_line=[term]
            for count in counts:
                matched=[t for t in tariffs if t.device_limit==count and t.months==month]
                values=[]
                for t in matched:
                    if values:values.append('\n')
                    cell=price_text(t.price,prices[t.id],rich=True)
                    values.extend(cell if isinstance(cell,list) else [cell])
                line.append(values or '—')
                plain_line.append('\n'.join(price_text(t.price,prices[t.id],html=True) for t in matched) or '—')
            table.append(line);plain.append(plain_line)
        blocks.extend(b for b in rich_table('', '',headers,table).blocks if b.type=='table')
        fallback.append(' · '.join(headers)+'\n'+'\n'.join(' · '.join(r) for r in plain))
    blocks.append(dict(type='paragraph',text=note))
    rich=InputRichMessage(blocks=blocks,skip_entity_detection=True)
    rows=[[(f'📱 {devices_label(n)}',f'renew_devices:{sid}:{n}' if sid else f'buy_devices:{n}')] for n in shown]
    if offset:rows.append([('← Предыдущие устройства',f'catalog_groups:{sid}:{offset-SIZE}')])
    if offset+SIZE<len(groups):rows.append([('Следующие устройства →',f'catalog_groups:{sid}:{offset+SIZE}')])
    from app.services.trial_offer import eligible as trial_eligible
    if not sid and mode in ('buy','catalog_groups') and await trial_eligible(repo,settings,user):
        rows.insert(0,[('🎁 Попробовать бесплатно','trial')])
    if sid:rows.append([('💳 Автопродление и способ оплаты',f'autorenew:{sid}')])
    if not sid and settings.is_owner(user.telegram_id):
        rows.append([('⚙️ Свой срок и устройства',f'admin:grantconfig:new:{user.id}:buy')])
    rows.append([('← Назад',f'sub:{sid}' if sid else 'home')])
    text='<b>'+title+'</b>\n\n'+escape(intro)+'\n\n'+'\n\n'.join(fallback)+'\n\n'+escape(note)
    return Screen(text,rows,rich=rich,structured=True,appearance_page='buy')
