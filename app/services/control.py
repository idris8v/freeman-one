"""Persistent customization, campaigns, manual withdrawals and reports."""
import hashlib
import json
import re
import secrets
from datetime import timedelta
from html import escape
from decimal import Decimal, InvalidOperation
from cryptography.fernet import Fernet
from sqlalchemy import select, update, func
from app.database.models import (User, Subscription, Payment, Tariff, BalanceTransaction,
    Campaign, Delivery, Withdrawal, ReferralReward, Expense, AdminAudit, AccessKey, FunnelEvent, now)
from app.services.screens import Screen, money, duration

from app.config.admin_content import MEDIA_PAGES,MEDIA_GROUPS
PAGES=list(MEDIA_PAGES)
PAGE_NAMES=dict(MEDIA_PAGES)
AUDIENCES = {
    'all': 'Все пользователи',
    'active': 'С активной подпиской',
    'expired': 'Подписка истекла',
    'never': 'Подписки ещё не было',
    'inactive': 'Все без активной подписки',
    'lapsed30': 'Не покупали VPN 30 дней · без активной подписки',
}

FIELDS = {'reminder_hours': (1, 720, 72), 'reminders_enabled': (0, 1, 1),
          'reminder_trials': (0, 1, 0), 'withdraw_min': (1, 1000000, 1000),
          'withdraw_enabled': (0, 1, 1), 'referral_first_only': (0, 1, 1),
          'referral_fixed': (0, 1000000, 0),
          'backup_offsite_enabled': (0, 1, 1)}


class ControlService:
    def __init__(self, repo, settings):
        self.repo, self.settings, self.session = repo, settings, repo.session

    def admin(self, actor):
        if not self.settings.is_admin(actor.telegram_id):
            raise PermissionError('Admin access denied')

    def cipher(self):
        if not self.settings.encryption_key:
            raise ValueError('Вывод временно недоступен: ключ шифрования не настроен.')
        return Fernet(self.settings.encryption_key.encode())

    async def audit(self, actor, action, target):
        await self.repo.add(AdminAudit(actor_telegram_id=actor.telegram_id, action=action,
                                     target=str(target), details='Изменение через панель управления'))

    async def audience(self, name):
        if name not in AUDIENCES:
            raise ValueError('Неизвестная группа.')
        active,expired,history=set(),set(),set()
        if name!='all':
            stamp=now()
            from app.services.vless_creation import utc
            subscriptions=await self.session.scalars(select(Subscription))
            for sub in subscriptions:
                history.add(sub.user_id)
                if utc(sub.expires_at)<=stamp:
                    expired.add(sub.user_id)
                elif (await self.repo.setting(f'subscription_status:{sub.id}','active')=='active'
                      and await self.repo.setting(f'subscription_removed:{sub.id}')!='1'
                      and await self.repo.setting(f'subscription_transferred:{sub.id}')!='1'):
                    active.add(sub.user_id)
        archived = {int(key.split(':', 1)[1]) for key, value in (await self.repo.settings_prefix('user_archived:')).items()
                    if value == '1' and key.split(':', 1)[1].isdigit()}
        query = select(User).where(User.status != 'blocked',User.telegram_id>0)
        if archived:
            query = query.where(User.id.not_in(archived))
        users = await self.session.scalars(query.order_by(User.id))
        if name=='all':
            return list(users)
        if name=='inactive':
            return [u for u in users if u.id not in active]
        if name=='never':
            return [u for u in users if u.id not in history]
        if name=='lapsed30':
            old_buyers = set(await self.session.scalars(
                select(Payment.user_id)
                .where(Payment.subscription_id.is_not(None), Payment.amount>0,
                       Payment.status.in_(('succeeded','order_paid','stars_paid','balance_paid','manual_paid')))
                .group_by(Payment.user_id)
                .having(func.max(Payment.created_at)<=stamp-timedelta(days=30))
            ))
            return [u for u in users if u.id in old_buyers and u.id not in active]
        wanted=active if name=='active' else expired-active
        return [u for u in users if u.id in wanted]

    async def campaign_signature(self,item,recipients):
        from app.services.campaign_buttons import data
        from app.services.campaign_content import load

        snapshot=[item.audience,item.text,item.photo,await load(self.repo,item.id),
                  await data(self.repo,item.id),[(u.id,u.telegram_id) for u in recipients]]
        return hashlib.sha256(json.dumps(snapshot,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

    async def campaign_preview(self,item):
        from app.services.campaign_content import preview

        screen=await preview(self.repo,item)
        if item.status=='draft' and item.audience in AUDIENCES:
            screen.rows.insert(-1,[('🔘 Кнопки рассылки',f'admin:x:campaignbuttons:{item.id}')])
            recipients=await self.audience(item.audience)
            if recipients:
                token=secrets.token_hex(8)
                signature=await self.campaign_signature(item,recipients)
                await self.repo.set_setting(f'campaign_confirm:{item.id}',f'{token}:{signature}')
                action=f'admin:x:send:{item.id}:{token}'
                screen.rows.insert(-1,[(f'✅ Утвердить и отправить · {len(recipients)}',action)])
                screen.button_options[action]={'style':'success'}
            else:
                screen.rows.insert(-1,[('В группе нет получателей',f'admin:x:campaignaudience:{item.id}')])
        return screen

    async def campaign(self, id, actor):
        self.admin(actor)
        item = await self.session.get(Campaign, int(id),with_for_update=True)
        if not item or item.actor_id != actor.telegram_id or await self.repo.setting(f'campaign_hidden:{item.id}')=='1':
            raise ValueError('Рассылка не найдена.')
        return item

    async def render(self, action, actor):
        p = action.split(':')
        admin = p[0] == 'admin'
        if admin:
            self.admin(actor)
            if not self.settings.can_admin_action(actor.telegram_id, action):
                raise PermissionError('Недостаточно прав для этого действия.')
            p = p[2:]
        else:
            p = p[1:]
        origin = 'balance'
        if not admin and p[-1] in ('from_referral','from_balance'):
            origin = p.pop()[5:]
        suffix = f':from_{origin}' if not admin else ''
        page = p[0]
        marketing_pages = {'campaigns', 'campaign', 'send', 'cancel', 'withdrawals', 'payout', 'payoutconfirm', 'payoutapply', 'reminders', 'referrals'}
        commerce_pages = {'report', 'funnel', 'export'}
        parent = 'admin:marketing' if page in marketing_pages else 'admin:commerce' if page in commerce_pages else 'admin:x:appearance' if page in ('content','instructionitem','faqitem','documentitem','screen','media_group','input') else 'admin:x:home'
        back = [('← Назад', parent if admin else origin)]
        if admin and page == 'home':
            return Screen('🛠 <b>Управление ботом</b>', [
                [('📖 Контент и инструкции', 'admin:x:content')],
                [('🔔 Напоминания', 'admin:x:reminders'), ('👥 Реферальные правила', 'admin:x:referrals')],
                [('← Назад', 'admin:home')]])
        if admin and page == 'monitor':
            enabled=await self.repo.setting('backup_offsite_enabled','1')
            last=await self.repo.setting('backup_offsite_last','ещё не отправлялась')
            return Screen(f'🛡 <b>Мониторинг и резервные копии</b>\n\nПроверка сервера: каждые 5 минут; уведомление владельцу при сбое и восстановлении.\nЛокальная копия: ежедневно, хранение 7 дней.\nЗашифрованная копия владельцу: {"включена" if enabled=="1" else "выключена"}.\nПоследняя успешная отправка: {escape(last)} UTC.\n\nФайл зашифрован ключом ENCRYPTION_KEY сервера. Сохраните этот ключ отдельно.',[[('Включить / выключить копию владельцу','admin:x:input:setting:backup_offsite_enabled')],[('← Назад','admin:more')]])
        if admin and page in ('campaigndelete','campaigndeleteconfirm'):
            item=await self.campaign(p[1],actor)
            if item.status not in ('draft','cancelled'):
                raise ValueError('Сначала отмените рассылку. Отправленные рассылки сохраняются в истории.')
            if page=='campaigndeleteconfirm':
                await self.repo.set_setting(f'campaign_hidden:{item.id}','1')
                await self.audit(actor,'campaign_hide',item.id)
                return await self.render('admin:x:campaigns:0',actor)
            return Screen('🗑 <b>Удалить рассылку из списка?</b>\n\nОна не будет отправлена. История доставки сохранится.',[[('🗑 Удалить',f'admin:x:campaigndeleteconfirm:{item.id}')],[('← Назад',f'admin:x:campaign:{item.id}')]],structured=True)
        if admin and page.startswith('campaign') and page not in ('campaigns','campaign','campaignnew','campaignpreview','campaignaudience','campaignset','campaignrecipients'):
            from app.services.campaign_buttons import render
            return await render(self,action,actor)
        if admin and page == 'campaigns':
            offset = max(0, int(p[1]))
            hidden=[int(k.split(':')[1]) for k,v in (await self.repo.settings_prefix('campaign_hidden:')).items() if v=='1' and k.split(':')[1].isdigit()]
            query=select(Campaign).where(Campaign.audience.in_(tuple(AUDIENCES)))
            if hidden:query=query.where(Campaign.id.not_in(hidden))
            items = list(await self.session.scalars(query.order_by(Campaign.id.desc()).offset(offset).limit(6)))
            rows = [[(f'#{c.id} · {AUDIENCES.get(c.audience,"Служебная рассылка")} · {c.status}', f'admin:x:campaign:{c.id}')] for c in items[:5]]
            if offset:
                rows.append([('← Предыдущие', f'admin:x:campaigns:{max(0, offset-5)}')])
            if len(items) > 5:
                rows.append([('Следующие →', f'admin:x:campaigns:{offset+5}')])
            return Screen('📣 <b>Рассылки</b>\n\nВыберите группу и пришлите готовый пост, Rich Message или фото с подписью. Проверьте предпросмотр перед отправкой.',
                [[('➕ Создать рассылку','admin:x:campaignnew')]] + rows + [back],structured=True)
        if admin and page=='campaignnew':
            return Screen('➕ <b>Новая рассылка</b>\n\nКому отправить?\n\nДалее пришлите текст с форматированием, фото с подписью или Rich Message. Можно сначала прислать фото, затем текст. До подтверждения ничего не отправляется.',[[(label,f'admin:x:input:campaign:{key}')] for key,label in AUDIENCES.items()]+[[('← Назад','admin:x:campaigns:0')]],structured=True)
        if admin and page=='campaignrecipients':
            item=await self.campaign(p[1],actor)
            offset=max(0,int(p[2])) if len(p)>2 else 0
            jobs=list(await self.session.scalars(select(Delivery).where(Delivery.campaign_id==item.id).order_by(Delivery.id).offset(offset).limit(6)))
            lines=[]
            for job in jobs[:5]:
                person=await self.session.get(User,job.user_id)
                lines.append(f'{escape(person.display_name if person else "Клиент удалён")} · {job.status}')
            rows=[]
            if offset:rows.append([('← Предыдущие',f'admin:x:campaignrecipients:{item.id}:{max(0,offset-5)}')])
            if len(jobs)>5:rows.append([('Следующие →',f'admin:x:campaignrecipients:{item.id}:{offset+5}')])
            return Screen('👥 <b>Доставка получателям</b>\n\n'+('\n\n'.join(lines) or 'Отправка ещё не началась.'),rows+[[('← К рассылке',f'admin:x:campaign:{item.id}')]],structured=True)
        if admin and page in ('campaignpreview','campaignaudience','campaignset'):
            item=await self.campaign(p[1],actor)
            if page=='campaignpreview':
                return await self.campaign_preview(item)
            if item.status!='draft':raise ValueError('Получателей можно менять только до отправки.')
            if page=='campaignset':
                if p[2] not in AUDIENCES:raise ValueError('Группа недоступна.')
                item.audience=p[2]
                return await self.render(f'admin:x:campaign:{item.id}',actor)
            return Screen('👥 Выберите получателей:',[[(label,f'admin:x:campaignset:{item.id}:{key}')] for key,label in AUDIENCES.items()]+[[('← Назад',f'admin:x:campaign:{item.id}')]],structured=True)
        if admin and page in ('campaign', 'send', 'cancel'):
            item = await self.campaign(p[1], actor)
            if page == 'send':
                if item.audience not in AUDIENCES:raise ValueError('Служебные уведомления нельзя отправлять повторно.')
                if item.status=='draft':
                    recipients=await self.audience(item.audience)
                    if not recipients or len(p)!=3 or not re.fullmatch(r'[0-9a-f]{16}',p[2]):
                        return await self.campaign_preview(item)
                    signature=await self.campaign_signature(item,recipients)
                    saved=await self.repo.setting(f'campaign_confirm:{item.id}')
                    if saved!=f'{p[2]}:{signature}':
                        return Screen('Черновик или состав получателей изменился. Проверьте новый предпросмотр.',
                                      [[('👁 Предпросмотр',f'admin:x:campaignpreview:{item.id}')]],structured=True)
                    claimed=await self.session.execute(update(Campaign).where(
                        Campaign.id==item.id,Campaign.status=='draft').values(status='queued'))
                else:
                    claimed=None
                if claimed is not None and claimed.rowcount:
                    for u in recipients:
                        await self.repo.add(Delivery(dedup=f'campaign:{item.id}:{u.id}', user_id=u.id,
                            campaign_id=item.id, text=item.text, photo=item.photo))
                    await self.audit(actor, 'campaign_queue', item.id)
                await self.session.refresh(item)
            if page == 'cancel':
                await self.session.execute(update(Campaign).where(Campaign.id == item.id, Campaign.status.in_(['draft', 'queued'])).values(status='cancelled'))
                await self.session.execute(update(Delivery).where(Delivery.campaign_id == item.id, Delivery.status == 'pending').values(status='cancelled'))
                await self.audit(actor, 'campaign_cancel', item.id)
                await self.session.refresh(item)
            stats = dict((await self.session.execute(select(Delivery.status, func.count()).where(Delivery.campaign_id == item.id).group_by(Delivery.status))).all())
            rows = []
            count = len(await self.audience(item.audience)) if item.status == 'draft' and item.audience in AUDIENCES else sum(stats.values())
            if item.status!='draft':rows.append([('👥 Доставка получателям',f'admin:x:campaignrecipients:{item.id}:0')])
            rows.append([('👁 Предпросмотр',f'admin:x:campaignpreview:{item.id}')])
            if item.status == 'draft' and item.audience in AUDIENCES:
                rows.append([('👥 Изменить получателей',f'admin:x:campaignaudience:{item.id}')])
                rows.append([('🔘 Кнопки рассылки',f'admin:x:campaignbuttons:{item.id}')])
                rows.append([('✏️ Изменить сообщение',f'admin:x:input:campaignedit:{item.id}')])
            if item.status in ('draft', 'queued'):
                rows.append([('🛑 Отменить', f'admin:x:cancel:{item.id}')])
            if item.status in ('draft','cancelled'):
                rows.append([('🗑 Удалить рассылку',f'admin:x:campaigndelete:{item.id}')])
            text = f'📣 #{item.id} · {AUDIENCES.get(item.audience,"Служебная рассылка")}\nСтатус: {item.status}\nПолучателей: {count}\nДоставлено: {stats.get("sent",0)} · Ошибок: {stats.get("failed",0)} · Пропущено: {stats.get("skipped",0)}\nВ очереди: {stats.get("pending",0)}\n\n' + escape(item.text)
            return Screen(text, rows + [[('🔄 Обновить', f'admin:x:campaign:{item.id}')], [('← Назад', 'admin:x:campaigns:0')]],structured=True)
        if admin and page == 'appearance':
            return Screen('🎨 <b>Оформление и контент</b>\n\nЗдесь собраны медиа экранов, тексты, инструкции, документы и настройки кнопок.',[[('🖼 Экраны: фото, GIF и видео','admin:x:appearance_screens')],[('🔘 Кнопки, видимость и порядок','admin:buttons:0')],[('📚 Тексты, FAQ, инструкции и документы','admin:x:content')],[('📝 Приветствие, бренд и поддержка','admin:texts')],[('← Назад','admin:home')]])
        if admin and page=='richpreview':
            from aiogram.types import (RichMessage,RichBlockSectionHeading,RichBlockParagraph,
                RichBlockTable,RichBlockTableCell,RichBlockDetails,RichBlockButtons,
                RichMessageButton,CopyTextButton)
            rich=RichMessage(blocks=[
                RichBlockSectionHeading(text='F R E E M A N',size=2),
                RichBlockParagraph(text='Оставь границы позади.'),
                RichBlockTable(cells=[
                    [RichBlockTableCell(align='left',valign='middle',text='Статус',is_header=True),RichBlockTableCell(align='right',valign='middle',text='Активен')],
                    [RichBlockTableCell(align='left',valign='middle',text='Тариф'),RichBlockTableCell(align='right',valign='middle',text='Пробный')],
                    [RichBlockTableCell(align='left',valign='middle',text='Осталось'),RichBlockTableCell(align='right',valign='middle',text='24 часа')],
                ],is_bordered=True,is_striped=True,is_compact=True),
                RichBlockDetails(summary='Как подключиться',blocks=[RichBlockParagraph(text='Откройте карточку подписки, отсканируйте QR-код или скачайте конфигурацию.')]),
                RichBlockButtons(buttons=[
                    RichMessageButton(text='Открыть меню',callback_data='home',style='primary'),
                    RichMessageButton(text='Скопировать пример',copy_text=CopyTextButton(text='FREEMAN-DEMO')),
                ],align='center'),
            ])
            return Screen('Rich Message недоступно на этом клиенте. Откройте обновлённый Telegram.',[],rich=rich)
        if admin and page == 'appearance_screens':
            return Screen('🖼 <b>Фото и видео по разделам</b>\n\nВыберите раздел и экран. На карточках с персональным QR-кодом медиа показывается отдельно: QR-код сохраняется.',[[(title,f'admin:x:media_group:{key}')] for key,(title,pages) in MEDIA_GROUPS.items()]+[[('← Назад','admin:x:appearance')]])
        if admin and page=='media_group':
            group=p[1]
            if group not in MEDIA_GROUPS:raise ValueError('Раздел недоступен.')
            title,pages=MEDIA_GROUPS[group]
            return Screen('<b>'+escape(title)+'</b>',[[(PAGE_NAMES[key],f'admin:x:screen:{key}')] for key in pages[max(0,int(p[2])) if len(p)>2 else 0:(max(0,int(p[2])) if len(p)>2 else 0)+5]]+([ [('← Предыдущие',f'admin:x:media_group:{group}:{max(0,int(p[2])-5)}')] ] if len(p)>2 and int(p[2])>0 else [])+([ [('Следующие →',f'admin:x:media_group:{group}:{(int(p[2]) if len(p)>2 else 0)+5}')] ] if (int(p[2]) if len(p)>2 else 0)+5<len(pages) else [])+[[('← Разделы','admin:x:appearance_screens')]])
        if admin and page == 'screen':
            key = p[1]
            if key not in PAGES:
                raise ValueError('Экран недоступен.')
            media_type=await self.repo.setting('media_type:'+key)
            if not media_type and await self.repo.setting('image:'+key): media_type='photo'
            media_name={'photo':'фотография','animation':'GIF-анимация','video':'видео'}.get(media_type,'нет')
            return Screen(f'Экран: {key} — {PAGE_NAMES[key]}\nМедиа: {media_name}\nВступление:\n{escape(await self.repo.setting("intro:"+key) or "—")}',
                [[('🎞 Загрузить / убрать медиа', f'admin:x:input:media:{key}')], [('✏️ Вступление', f'admin:x:input:intro:{key}')], [('← Назад', 'admin:x:media_group:'+next((g for g,(_,pages) in MEDIA_GROUPS.items() if key in pages),'menu'))]])
        if admin and page == 'content':
            from app.config.content import INSTRUCTIONS, FAQ, DOCUMENTS
            rows = [[('Тариф '+duration(t.months), f'admin:x:input:description:id{t.id}')] for t in await self.repo.tariffs() if await self.repo.tariff_setting(t, 'tariff_deleted')!='1']
            rows += [[('📱 '+((await self.repo.setting('instruction_title:'+key)) or title),f'admin:x:instructionitem:{key}')] for key,(title,_) in INSTRUCTIONS.items()]
            rows += [[('❓ '+((await self.repo.setting(f'faq_question:{i}')) or question)[:50],f'admin:x:faqitem:{i}')] for i,(question,_) in enumerate(FAQ)]
            rows += [[('📄 '+((await self.repo.setting(f'document_title:{i}')) or title)[:50],f'admin:x:documentitem:{i}')] for i,title in enumerate(DOCUMENTS)]
            categories={'tariffs':('💳 Описания тарифов','description'),'instructions':('📱 Инструкции','instructionitem'),'faq':('❓ Частые вопросы','faqitem'),'documents':('📄 Документы','documentitem')}
            group=p[1] if len(p)>1 else ''
            if group not in categories:
                return Screen('📚 <b>Тексты и документы</b>\n\nВыберите тип текста. В редакторе показан текущий текст: его можно скопировать и поправить. Описания тарифов не меняют цены и лимиты.',[[(title,'admin:x:content:'+key)] for key,(title,_) in categories.items()]+[[('← Назад','admin:x:appearance')]],structured=True)
            title,marker=categories[group];rows=[row for row in rows if marker in row[0][1]]
            offset=max(0,int(p[2])) if len(p)>2 else 0
            nav=[]
            if offset:nav.append([('← Предыдущие',f'admin:x:content:{group}:{max(0,offset-5)}')])
            if offset+5<len(rows):nav.append([('Следующие →',f'admin:x:content:{group}:{offset+5}')])
            return Screen('<b>'+title+'</b>\n\nВыберите текст для редактирования.',rows[offset:offset+5]+nav+[[('← Назад','admin:x:content')]],structured=True)
        if admin and page=='instructionitem':
            from app.config.content import INSTRUCTIONS
            key=p[1]
            if key not in INSTRUCTIONS: raise ValueError('Инструкция недоступна.')
            default_title,default_text=INSTRUCTIONS[key]
            title=(await self.repo.setting('instruction_title:'+key)) or default_title;body=(await self.repo.setting('instruction:'+key)) or default_text
            return Screen(f'📱 <b>{escape(title)}</b>\n\n{escape(body)}',[[('✏️ Изменить название',f'admin:x:input:instruction_title:{key}')],[('📝 Изменить инструкцию',f'admin:x:input:instruction:{key}')],[('← Назад','admin:x:content')]])
        if admin and page=='faqitem':
            from app.config.content import FAQ
            index=int(p[1])
            if not 0<=index<len(FAQ): raise ValueError('Вопрос недоступен.')
            default_question,default_answer=FAQ[index]
            question=(await self.repo.setting(f'faq_question:{index}')) or default_question;answer=(await self.repo.setting(f'faq:{index}')) or default_answer
            return Screen(f'❓ <b>{escape(question)}</b>\n\n{escape(answer)}',[[('✏️ Изменить вопрос',f'admin:x:input:faq_question:{index}')],[('📝 Изменить ответ',f'admin:x:input:faq:{index}')],[('← Назад','admin:x:content')]])
        if admin and page=='documentitem':
            from app.config.content import DOCUMENTS,DOCUMENT_TEXTS
            index=int(p[1])
            if not 0<=index<len(DOCUMENTS): raise ValueError('Документ недоступен.')
            title=(await self.repo.setting(f'document_title:{index}')) or DOCUMENTS[index];body=(await self.repo.setting(f'document:{index}')) or DOCUMENT_TEXTS[index]
            return Screen(f'📄 <b>{escape(title)}</b>\n\n{escape(body)}',[[('✏️ Изменить название',f'admin:x:input:document_title:{index}')],[('📝 Изменить документ',f'admin:x:input:document:{index}')],[('← Назад','admin:x:content')]])
        if admin and page in ('reminders', 'referrals'):
            keys = ['reminders_enabled', 'reminder_hours', 'reminder_trials'] if page == 'reminders' else ['withdraw_enabled', 'withdraw_min']
            names = {'reminders_enabled': 'Напоминания: 0 выкл., 1 вкл.', 'reminder_hours': 'За сколько часов (72 = 3 дня)',
                'reminder_trials': 'Напоминать о пробном: 0/1', 'referral_first_only': 'Только первая покупка: 0/1',
                'referral_fixed': 'Фиксированный бонус в ₽ (0 = процент)', 'withdraw_enabled': 'Заявки на вывод: 0/1', 'withdraw_min': 'Минимум вывода, ₽'}
            text = '\n'.join([f'{names[k]}: {await self.repo.setting(k, str(FIELDS[k][2]))}' for k in keys])
            if page == 'referrals':
                text += '\n\nБонус только с подтверждённых покупок. Самоприглашение исключено. Вывод — ручная выплата администратором. Изменения правил применяются к последующим начислениям.'
            return Screen(text, [[(names[k], f'admin:x:input:setting:{k}')] for k in keys] + ([ [('Уровни / минимум покупки', 'admin:referrals')] ] if page == 'referrals' else []) + [[('← Назад','admin:referrals' if page=='referrals' else 'admin:marketing')]],structured=True)
        if not admin and page == 'notifications':
            value = await self.repo.setting(f'notifications:{actor.id}', '1')
            return Screen('Напоминания о подписке: '+('включены' if value == '1' else 'выключены'), [[('Включить / выключить', 'x:notifytoggle')], [('← Назад', 'help')]])
        if not admin and page == 'notifytoggle':
            key = f'notifications:{actor.id}'
            await self.repo.set_setting(key, '0' if await self.repo.setting(key, '1') == '1' else '1')
            return await self.render('x:notifications', actor)
        if not admin and page == 'withdraw':
            balance = await self.repo.balance(actor.id)
            minimum = int(await self.repo.setting('withdraw_min', '1000')) * 100
            if await self.repo.setting('withdraw_enabled', '1') != '1':
                return Screen('Заявки на вывод временно отключены.', [back])
            if balance < minimum:
                return Screen(f'Доступно: {money(balance)}\n\nМинимум для вывода: {money(minimum)}', [back], structured=True)
            self.cipher()
            return Screen(f'💸 <b>Заявка на вывод</b>\n\nДоступно: {money(balance)}\nСумма будет рассчитана автоматически.\nУкажите банк | карта или телефон | ФИО\nПример: Банк | +79991234567 | Иван Иванов\nНе указывайте CVV, PIN или срок действия.\nВыплата производится администратором вручную.', [[('✍️ Указать реквизиты', 'x:input:withdraw'+suffix)], [back]])
        if not admin and page in ('withdrawconfirm', 'withdrawapply'):
            item = await self.session.get(Withdrawal, int(p[1]))
            if not item or item.user_id != actor.id:
                raise ValueError('Заявка не найдена.')
            if page == 'withdrawapply':
                await self.session.execute(update(User).where(User.id == actor.id).values(status=User.status))
                await self.session.refresh(item)
                if item.status == 'draft' and (await self.repo.setting('withdraw_enabled', '1') != '1' or await self.repo.balance(actor.id) < item.amount or item.amount < int(await self.repo.setting('withdraw_min','1000'))*100):
                    raise ValueError('Доступный баланс или правила изменились. Создайте новую заявку.')
                changed = await self.session.execute(update(Withdrawal).where(Withdrawal.id == item.id, Withdrawal.status == 'draft', Withdrawal.created_at > now()-timedelta(minutes=15)).values(status='pending').execution_options(synchronize_session='fetch'))
                if changed.rowcount:
                    await self.repo.add(BalanceTransaction(user_id=actor.id, type='withdrawal_hold', amount=-item.amount, description=f'Резерв на вывод #{item.id}'))
                    for id in self.settings.admin_ids.split(','):
                        admin_user = await self.repo.user(int(id.strip())) if id.strip().isdigit() else None
                        if admin_user:
                            await self.repo.add(Delivery(dedup=f'withdrawal:{item.id}:admin:{admin_user.id}',user_id=admin_user.id,text=f'💸 Новая заявка на вывод #{item.id}: {money(item.amount)}. Клиент {actor.telegram_id}. Откройте главный экран → Заявки на выплату.'))
                elif item.status == 'draft':
                    raise ValueError('Подтверждение устарело. Создайте новую заявку.')
                await self.session.refresh(item)
            rows = [[('✅ Подать заявку', f'x:withdrawapply:{item.id}{suffix}')]] if item.status == 'draft' else []
            details = self.cipher().decrypt(item.details.encode()).decode()
            return Screen(f'Заявка #{item.id}\nСумма: {money(item.amount)}\nСтатус: {item.status}\n{escape(details)}\n\nПосле подачи сумма резервируется. При отказе она вернётся на баланс.', rows + [back])
        if page == 'withdrawals':
            offset = max(0, int(p[1])) if len(p)>1 else 0
            query = select(Withdrawal).where(Withdrawal.status != 'draft')
            if not admin:
                query = query.where(Withdrawal.user_id == actor.id)
            items = list(await self.session.scalars(query.order_by(Withdrawal.id.desc()).offset(offset).limit(6)))
            prefix = 'admin:x:payout' if admin else 'x:withdrawconfirm'
            rows = [[(f'#{w.id} · {money(w.amount)} · {w.status}', f'{prefix}:{w.id}{suffix}')] for w in items[:5]]
            if offset:
                rows.append([('← Предыдущие', f'{"admin:x" if admin else "x"}:withdrawals:{max(0,offset-5)}{suffix}')])
            if len(items)>5:
                rows.append([('Следующие →', f'{"admin:x" if admin else "x"}:withdrawals:{offset+5}{suffix}')])
            if admin:
                minimum=int(await self.repo.setting('withdraw_min','1000'))
                pending=await self.session.scalar(select(func.count()).select_from(Withdrawal).where(Withdrawal.status=='pending')) or 0
                text=f'💸 <b>Заявки на выплату</b>\n\nОжидают обработки: {pending}\n\nМинимальная сумма вывода: {money(minimum*100)}\n\nВыберите заявку, чтобы проверить реквизиты и отметить выплату или отклонить её.'
                if not items:text+='\n\nЗаявок пока нет.'
                return Screen(text,rows+[[('⚙️ Минимальная сумма вывода','admin:x:input:setting:withdraw_min')],[('🔄 Обновить',f'admin:x:withdrawals:{offset}')],[('← На главный экран','home')]],structured=True)
            return Screen('💸 Заявки на вывод' if items else 'Заявок пока нет.', rows+[back])
        if admin and page in ('payout', 'payoutconfirm', 'payoutapply'):
            item = await self.session.get(Withdrawal, int(p[1]))
            if not item or item.status == 'draft':
                raise ValueError('Заявка не найдена.')
            if page == 'payoutapply':
                operation = p[2]
                if operation not in ('paid', 'rejected'):
                    raise ValueError('Неверное действие.')
                await self.session.execute(update(User).where(User.id == item.user_id).values(status=User.status))
                changed = await self.session.execute(update(Withdrawal).where(Withdrawal.id == item.id, Withdrawal.status == 'pending').values(status=operation, processed_at=now()))
                if changed.rowcount:
                    if operation == 'rejected':
                        await self.repo.add(BalanceTransaction(user_id=item.user_id, type='withdrawal_release', amount=item.amount, description=f'Возврат резерва #{item.id}'))
                    await self.audit(actor, 'withdrawal_'+operation, item.id)
                    await self.repo.add(Delivery(dedup=f'payout:{item.id}:result', user_id=item.user_id, text=f'Заявка на вывод #{item.id}: '+('выплачена' if operation == 'paid' else 'отклонена, резерв возвращён на баланс')+f'. Сумма: {money(item.amount)}'))
                await self.session.refresh(item)
            customer = await self.session.get(User, item.user_id)
            text = f'Заявка #{item.id} · {item.status}\nКлиент: {customer.telegram_id}\nСумма: {money(item.amount)}\n'+escape(self.cipher().decrypt(item.details.encode()).decode())
            rows = []
            if item.status == 'pending':
                if page == 'payoutconfirm':
                    operation = p[2]
                    if operation not in ('paid','rejected'):
                        raise ValueError('Неверное действие.')
                    text += '\n\n'+('Подтвердите, что вы уже перевели деньги клиенту. Бот не выполняет перевод.' if operation == 'paid' else 'Подтвердите отказ и возврат резерва.')
                    rows.append([('✅ Подтвердить', f'admin:x:payoutapply:{item.id}:{operation}')])
                else:
                    rows += [[('✅ Уже выплачено', f'admin:x:payoutconfirm:{item.id}:paid')], [('❌ Отклонить', f'admin:x:payoutconfirm:{item.id}:rejected')]]
            return Screen(text, rows+[[('👤 Клиент', f'admin:client:{item.user_id}')], [('← Назад', 'admin:x:withdrawals:0')]])
        if admin and page == 'report':
            return await self.report(int(p[1]))
        if admin and page == 'funnel':
            return await self.funnel(int(p[1]))
        if admin and page == 'exports':
            days = int(p[1])
            if days not in (0,1,7,30,90):
                raise ValueError('Период недоступен.')
            label = 'всё время' if not days else '24 часа' if days == 1 else f'{days} дней'
            return Screen(f'📤 <b>Выгрузки · {label}</b>\n\nExcel содержит отдельные листы: клиенты, подписки, ключи, платежи, тарифы и продажи по дням. CSV удобен для импорта в другие системы.', [
                [('📗 Полный отчёт Excel', f'admin:x:exportxlsx:{days}')],
                [('👥 Клиенты CSV', f'admin:x:exportclients:{days}'), ('🔑 Ключи CSV', f'admin:x:exportkeys:{days}')],
                [('💳 Платежи CSV', f'admin:x:export:{days}')],
                [('← Назад', f'admin:x:report:{days}')],
            ])
        if admin and page in ('export','exportclients','exportkeys','exportxlsx'):
            from app.services.reporting import ReportingService, csv_file, PERIODS
            days = int(p[1])
            if days not in PERIODS:
                raise ValueError('Период недоступен.')
            service = ReportingService(self.repo)
            marker = PERIODS[days]
            if page == 'exportxlsx':
                document = (await service.xlsx(days), f'freeman-report-{marker}.xlsx')
                text = 'Готов полный Excel-отчёт. Конфигурации VPN и платёжные секреты в него не включаются.'
            else:
                datasets = await service.datasets(days)
                title = {'export':'Платежи','exportclients':'Клиенты','exportkeys':'Ключи'}[page]
                headers, rows = datasets[title]
                document = (csv_file(headers, rows), f'freeman-{title.lower()}-{marker}.csv')
                text = f'Готова выгрузка «{title}» в CSV с кодировкой UTF-8.'
            return Screen(text, [[('← Назад',f'admin:x:exports:{days}')]], document=document)
        if page == 'input' and p[1] in ('campaignbutton','campaignbuttonlabel','campaignbuttonedit','campaignbuttonicon','campaignedit'):
            item=await self.campaign(p[2],actor)
            if item.status!='draft':raise ValueError('Редактирование доступно только в черновике.')
            prompt='Пришлите новое сообщение или фото с подписью. Форматирование сохраняется.' if p[1]=='campaignedit' else 'Введите новое название кнопки (до 64 символов):' if p[1]=='campaignbuttonlabel' else 'Введите: Название кнопки | '+('https-ссылка' if p[-1] in ('url','webapp') else 'текст поста / вложенного сообщения')+'\n\nПереносы строк сохраняются.'
            if p[1]=='campaignbuttonicon':prompt='Отправьте пользовательский эмодзи из набора Telegram или его числовой ID. Можно отправить стикер с custom emoji. Для удаления —.'
            if p[1]=='campaignbutton':
                from app.services.campaign_buttons import DEAL_INPUTS
                if p[-1] in DEAL_INPUTS:prompt='Введите название и значение через |.\n\n'+DEAL_INPUTS[p[-1]]
            if p[1]=='campaignbuttonedit':prompt='Введите: тип | Название | значение\n\nТипы: url — ссылка, webapp — Mini App, text — вложенный текст, post — пост в Mini App, discount — процент скидки, promo — действующий промокод.'
            return Screen(prompt,[[('← Назад',f'admin:x:campaign:{item.id}' if p[1]=='campaignedit' else f'admin:x:campaignbuttons:{item.id}')]],structured=True)
        if page == 'input':
            if not admin and p[1] != 'withdraw':
                raise PermissionError('Input denied')
            prompts = {'campaign':'Пришлите текст с форматированием (до 4096 символов), фото с подписью (до 1024) или Rich Message. Можно сначала прислать фото, затем текст. После этого появится предпросмотр и подтверждение отправки.',
                'image':'Пришлите фотографию. Для удаления отправьте —.', 'media':'Пришлите фотографию, GIF-анимацию или видео. Для удаления отправьте —.', 'intro':'Введите вступление (до 500 символов), — удалит текст.',
                'description':'Введите описание тарифа (до 1500 символов).', 'instruction':'Введите инструкцию (до 3500 символов). Отправьте — для возврата стандартного текста.',
                'instruction_title':'Введите название инструкции (до 120 символов). Отправьте — для возврата стандартного названия.',
                'faq':'Введите ответ (до 3500 символов). Отправьте — для возврата стандартного ответа.', 'faq_question':'Введите вопрос (до 200 символов). Отправьте — для возврата стандартного вопроса.',
                'document':'Введите текст документа (до 3500 символов). Отправьте — для возврата стандартного текста.', 'document_title':'Введите название документа (до 120 символов). Отправьте — для возврата стандартного названия.',
                'setting':'Введите целое число. 0/1 для переключателей.', 'expense':'Введите сумма в ₽ | описание расхода.',
                'withdraw':'Введите банк | карта или телефон | ФИО. Сумма равна доступному балансу.'}
            destination='admin:x:campaigns:0' if admin and p[1]=='campaign' else 'admin:x:referrals' if admin and p[1]=='setting' else parent if admin else origin
            kind=p[1];target=p[2] if len(p)>2 else ''
            prompt=prompts[kind]
            if admin and kind in ('intro','description','instruction','instruction_title','faq','faq_question','document','document_title'):
                from app.config.content import INSTRUCTIONS,FAQ,DOCUMENTS,DOCUMENT_TEXTS
                default=''
                if kind.startswith('instruction') and target in INSTRUCTIONS:default=INSTRUCTIONS[target][0 if kind.endswith('title') else 1]
                elif kind.startswith('faq'):default=FAQ[int(target)][0 if kind=='faq_question' else 1]
                elif kind.startswith('document'):default=DOCUMENTS[int(target)] if kind.endswith('title') else DOCUMENT_TEXTS[int(target)]
                current=await self.repo.setting(kind+':'+target,default)
                prompt+='\n\nТекущий текст (нажмите, чтобы скопировать):\n<code>'+escape(current or '—')+'</code>\n\nСкопируйте, измените и отправьте текст. Переносы строк сохраняются.'
            if admin:
                if kind in ('media','image','intro'):destination='admin:x:screen:'+target
                elif kind.startswith('instruction'):destination='admin:x:instructionitem:'+target
                elif kind.startswith('faq'):destination='admin:x:faqitem:'+target
                elif kind.startswith('document'):destination='admin:x:documentitem:'+target
                elif kind=='description':destination='admin:x:content'
            return Screen(prompt, [[('← Назад',destination)]],structured=True)
        raise ValueError('Раздел недоступен.')

    async def input(self, action, actor, text='', photo=None, media=None, content=None):
        p = action.split(':')
        admin = p[0] == 'admin'
        if admin:
            self.admin(actor)
            if not self.settings.is_owner(actor.telegram_id):
                raise PermissionError('Только владелец изменяет настройки.')
            p = p[3:]
        else:
            p = p[2:]
        kind, target = p[0], p[1] if len(p)>1 else ''
        text = text.strip()
        if admin and kind in ('campaignbutton','campaignbuttonlabel','campaignbuttonedit','campaignbuttonicon'):
            from app.services.campaign_buttons import input
            return await input(self,action,actor,text)
        if admin and kind=='campaignedit':
            item=await self.campaign(target,actor)
            if item.status!='draft':raise ValueError('Редактирование доступно только в черновике.')
            from app.web.posts import store_json
            item.text=text or 'Rich Message';item.photo=photo
            from app.services.campaign_content import load
            previous=await load(self.repo,item.id) or {}
            replacement=content or {'html':escape(text),'photo':photo}
            if previous.get('mode'):replacement['mode']=previous['mode']
            await store_json(self.repo,f'campaign_content:{item.id}',replacement)
            return await self.render(f'admin:x:campaign:{item.id}',actor)
        if not admin:
            if kind != 'withdraw':
                raise PermissionError('Input denied')
            if await self.repo.setting('withdraw_enabled','1') != '1':
                raise ValueError('Вывод отключён.')
            parts = [v.strip() for v in text.split('|')]
            if len(parts)!=3 or any(not v for v in parts) or len(text)>500:
                raise ValueError('Укажите банк | карта или телефон | ФИО.')
            account = re.sub(r'[\s()-]', '', parts[1])
            if not re.fullmatch(r'(?:\+\d{10,15}|\d{13,19})', account):
                raise ValueError('Нужен номер карты (13–19 цифр) или телефон с +кодом страны.')
            amount = await self.repo.balance(actor.id)
            if amount < int(await self.repo.setting('withdraw_min','1000'))*100:
                raise ValueError('Недостаточно средств для вывода.')
            item = await self.repo.add(Withdrawal(user_id=actor.id,amount=amount,details=self.cipher().encrypt(text.encode()).decode()))
            origin = target if target in ('from_referral','from_balance') else 'from_balance'
            return await self.render(f'x:withdrawconfirm:{item.id}:{origin}', actor)
        if kind == 'campaign':
            if target not in AUDIENCES or not (text or content and content.get('rich')) or len(text)>(1024 if photo else 4096):
                raise ValueError('Нужен текст до 3500 символов или фото с подписью до 800.')
            item = await self.repo.add(Campaign(actor_id=actor.telegram_id,audience=target,text=text or 'Rich Message',photo=photo))
            from app.web.posts import store_json
            await store_json(self.repo,f'campaign_content:{item.id}',content or {'html':escape(text),'photo':photo})
            await self.audit(actor,'campaign_draft',item.id)
            return await self.campaign_preview(item)
        if kind == 'setting':
            if target not in FIELDS or not text.isdigit():
                raise ValueError('Введите целое число.')
            low, high, _ = FIELDS[target]
            if not low<=int(text)<=high:
                raise ValueError(f'Допустимый диапазон: {low}–{high}.')
            await self.repo.set_setting(target,str(int(text)))
            destination = 'admin:x:withdrawals:0' if target=='withdraw_min' else 'admin:x:reminders' if target.startswith('reminder') else 'admin:x:monitor' if target=='backup_offsite_enabled' else 'admin:x:referrals'
        elif kind in ('image','media','intro','description','instruction','instruction_title','faq','faq_question','document','document_title'):
            from app.config.content import FAQ,DOCUMENTS
            from app.config.admin_content import VLESS_INSTRUCTIONS as INSTRUCTIONS
            if kind in ('image','media','intro'): valid=target in PAGES
            elif kind=='description': valid=bool(await self.repo.tariff(target))
            elif kind in ('instruction','instruction_title'): valid=target in INSTRUCTIONS
            elif kind in ('faq','faq_question'): valid=target.isdigit() and 0<=int(target)<len(FAQ)
            else: valid=target.isdigit() and 0<=int(target)<len(DOCUMENTS)
            if not valid:
                raise ValueError('Настройка недоступна.')
            if kind=='media':
                if not media and text!='—': raise ValueError('Пришлите фотографию, GIF или видео либо — для удаления.')
                media_type,media_file=media or ('','')
                await self.repo.set_setting('media_type:'+target,media_type);await self.repo.set_setting('media_file:'+target,media_file);await self.repo.set_setting('image:'+target,'')
                value=None
            elif kind=='image':
                if not photo and text!='—':
                    raise ValueError('Пришлите фото или — для удаления.')
                value = photo or ''
            else:
                limit=500 if kind=='intro' else 1500 if kind=='description' else 120 if kind in ('instruction_title','document_title') else 200 if kind=='faq_question' else 3500
                if not text or len(text)>limit:
                    raise ValueError('Текст пустой или слишком длинный.')
                value = '' if text=='—' else text
            if value is not None:
                if kind=='description':await self.repo.set_tariff_setting(await self.repo.tariff(target),'description',value)
                else:await self.repo.set_setting(kind+':'+target,value)
            destination=('admin:x:screen:'+target if kind in ('image','media','intro') else
                'admin:x:instructionitem:'+target if kind.startswith('instruction') else
                'admin:x:faqitem:'+target if kind.startswith('faq') else
                'admin:x:documentitem:'+target if kind.startswith('document') else 'admin:x:content')
        elif kind=='expense':
            parts=text.split('|',1)
            try:
                amount=Decimal(parts[0].strip().replace(',','.'))
                if not amount.is_finite() or amount<=0 or amount>1000000 or amount*100 != (amount*100).to_integral_value() or len(parts)!=2 or not parts[1].strip() or len(parts[1])>255:
                    raise ValueError()
            except (InvalidOperation,ValueError):
                raise ValueError('Введите положительную сумму до 1 000 000 ₽ | описание, до двух знаков после запятой.')
            await self.repo.add(Expense(amount=int(amount*100),description=parts[1].strip()))
            destination='admin:x:report:30'
        else:
            raise ValueError('Действие недоступно.')
        await self.audit(actor,'setting_'+kind,target)
        screen=await self.render(destination,actor)
        screen.text='✅ Сохранено.\n\n'+screen.text
        return screen

    async def funnel(self,days):
        if days not in (7,30,0):
            raise ValueError('Период недоступен.')
        since=now()-timedelta(days=days) if days else None
        async def stage(name):
            query=select(func.count(func.distinct(FunnelEvent.user_id))).where(FunnelEvent.stage==name)
            if since:
                query=query.where(FunnelEvent.created_at>=since)
            return await self.session.scalar(query)
        values={name:await stage(name) for name in ('start','plans','trial','checkout','paid')}
        def conversion(value,base):
            return f'{value/base*100:.1f}%' if base else '—'
        label='24 часа' if days==1 else f'{days} дней' if days else 'всё время'
        text=(f'🎯 <b>Воронка продаж · {label}</b>\n\n'
              f'Запустили бота: {values["start"]}\n'
              f'Открыли тарифы: {values["plans"]} · {conversion(values["plans"],values["start"])} от запусков\n'
              f'Активировали пробный: {values["trial"]} · {conversion(values["trial"],values["start"])}\n'
              f'Перешли к оплате: {values["checkout"]} · {conversion(values["checkout"],values["plans"])} от тарифов\n'
              f'Оплатили: {values["paid"]} · {conversion(values["paid"],values["checkout"])} от оплаты\n'
              f'Итоговая конверсия: {conversion(values["paid"],values["start"])}\n\n'
              'Каждый этап учитывается для клиента один раз. Статистика начинает накапливаться после установки этой функции.')
        rows=[[(label,f'admin:x:funnel:{n}') for label,n in [('Неделя',7),('Месяц',30),('Все',0)]],[('← Назад','admin:commerce')]]
        from aiogram.types import RichMessage,RichBlockSectionHeading,RichBlockParagraph,RichBlockTable,RichBlockTableCell
        def cell(value,header=False,align='left'):
            return RichBlockTableCell(align=align,valign='middle',text=str(value),is_header=header)
        rich=RichMessage(blocks=[
            RichBlockSectionHeading(text=f'Воронка продаж · {label}',size=2),
            RichBlockTable(cells=[
                [cell('Этап',True),cell('Клиентов',True,'right'),cell('Конверсия',True,'right')],
                [cell('Запустили бота'),cell(values['start'],False,'right'),cell('100%' if values['start'] else '—',False,'right')],
                [cell('Открыли тарифы'),cell(values['plans'],False,'right'),cell(conversion(values['plans'],values['start']),False,'right')],
                [cell('Пробный период'),cell(values['trial'],False,'right'),cell(conversion(values['trial'],values['start']),False,'right')],
                [cell('Перешли к оплате'),cell(values['checkout'],False,'right'),cell(conversion(values['checkout'],values['plans']),False,'right')],
                [cell('Оплатили'),cell(values['paid'],False,'right'),cell(conversion(values['paid'],values['checkout']),False,'right')],
            ],is_bordered=True,is_striped=True,is_compact=True),
            RichBlockParagraph(text=f'Итоговая конверсия: {conversion(values["paid"],values["start"])}. Каждый этап учитывается для клиента один раз.'),
        ])
        return Screen(text,rows,rich=rich)

    async def report(self, days):
        if days not in (1,7,30,90,0):
            raise ValueError('Период недоступен.')
        since=now()-timedelta(days=days) if days else None
        def period(query,model):
            return query.where(model.created_at>=since) if since else query
        archived = {int(key.split(':', 1)[1]) for key, value in (await self.repo.settings_prefix('user_archived:')).items()
                    if value == '1' and key.split(':', 1)[1].isdigit()}
        async def count(model,*conditions):
            query = period(select(func.count()).select_from(model).where(*conditions), model)
            if archived and model is User:
                query = query.where(User.id.not_in(archived))
            if archived and model is Subscription:
                query = query.where(Subscription.user_id.not_in(archived))
            if archived and model is AccessKey:
                query = query.where(AccessKey.user_id.not_in(archived))
            if archived and model is Payment:
                query = query.where(Payment.user_id.not_in(archived))
            return await self.session.scalar(query)
        successful=period(select(Payment).where(Payment.status.in_(['succeeded','order_paid','stars_paid','refunded'])),Payment)
        if archived:
            successful = successful.where(Payment.user_id.not_in(archived))
        pays=list(await self.session.scalars(successful))
        sales_query=period(select(Payment).where(Payment.status.in_(['succeeded','order_paid','stars_paid','refunded','balance_paid'])),Payment)
        if archived:
            sales_query=sales_query.where(Payment.user_id.not_in(archived))
        sales=list(await self.session.scalars(sales_query))
        topups=await self.session.scalar(period(select(func.coalesce(func.sum(Payment.amount),0)).where(Payment.status=='topup_credited'),Payment))
        revenue=sum(pay.amount for pay in pays)+topups
        costs=await self.session.scalar(period(select(func.coalesce(func.sum(Expense.amount),0)),Expense))
        rewards=await self.session.scalar(period(select(func.coalesce(func.sum(ReferralReward.amount),0)),ReferralReward))
        paidout=await self.session.scalar(select(func.coalesce(func.sum(Withdrawal.amount),0)).where(Withdrawal.status=='paid', *([Withdrawal.processed_at>=since] if since else [])))
        reserved=await self.session.scalar(select(func.coalesce(func.sum(Withdrawal.amount),0)).where(Withdrawal.status=='pending'))
        refunds=await self.session.scalar(period(select(func.coalesce(func.sum(Payment.amount),0)).where(Payment.status=='refunded'),Payment))
        wallet=await self.session.scalar(period(select(func.coalesce(func.sum(Payment.amount),0)).where(Payment.status=='balance_paid'),Payment))
        groups={}
        for pay in sales:
            sub=await self.session.get(Subscription,pay.subscription_id) if pay.subscription_id else None
            tariff=await self.session.get(Tariff,sub.tariff_id) if sub and sub.tariff_id else None
            name=await self.repo.tariff_setting(tariff, 'tariff_name',duration(tariff.months)) if tariff else 'Без привязки к тарифу'
            key=tariff.id if tariff else 0
            group=groups.setdefault(key,[name,0,0,set(),0])
            group[1]+=1; group[2]+=pay.amount; group[3].add(pay.user_id)
            if pay.status=='refunded':
                group[4]+=pay.amount
        label='24 часа' if days==1 else f'{days} дней' if days else 'всё время'
        active=len(await self.audience('active'))
        text=(f'📊 <b>Отчёт о продажах · {label}</b>\n\n'
            f'<b>💰 Деньги</b>\nВыручка: {money(revenue)}\nВозвраты: {money(refunds)}\nЧистая выручка: {money(revenue-refunds)}\nСредний чек продажи: {money(sum(item.amount for item in sales)//len(sales) if sales else 0)}\n'
            f'Из них пополнений ЮKassa: {money(topups)}\nРасходы: {money(costs)}\nРеферальные начисления: {money(rewards)}\n<b>Результат до налогов: {money(revenue-refunds-costs-rewards)}</b>\n\n'
            f'<b>🧾 Заказы</b>\nПродаж подписок и подарков: {len(sales)}\nУникальных покупателей: {len({p.user_id for p in sales})}\n'
            f'Ожидают оплаты: {await count(Payment,Payment.status.in_(("pending","topup_pending","order_pending","order_processing","stars_created","stars_pending","stars_processing")))}\nНеуспешных: {await count(Payment,Payment.status.in_(("failed","topup_canceled","order_canceled")))}\n'
            f'Покупки с внутреннего баланса: {money(wallet)}\n\n'
            f'<b>👥 Клиенты и доступы</b>\nНовых клиентов: {await count(User)}\nВыдано подписок: {await count(Subscription)} · пробных: {await count(Subscription,Subscription.kind=="trial")}\n'
            f'Создано ключей: {await count(AccessKey)}\nС активной подпиской сейчас: {active}\n\n'
            f'<b>🏦 Выплаты</b>\nВыплачено клиентам: {money(paidout)}\nРезерв на вывод сейчас: {money(reserved)}\n\n'
            f'<b>📦 По тарифам</b>\n')
        for name,n,total,buyers,returned in sorted(groups.values(),key=lambda g:g[1],reverse=True):
            text+=f'{escape(name)}: {n} · {money(total)} · покупателей {len(buyers)} · возвраты {money(returned)} · чистая выручка {money(total-returned)}\n'
        text += ('Продаж пока нет.\n' if not groups else '')+'\nРеальные VPN-подключения пока не измеряются. Период по датам создания, UTC; выплаты по дате обработки. Названия старых продаж отображаются по текущему тарифу. Расходы вносит администратор; это управленческий расчёт.'
        rows=[[(label,f'admin:x:report:{n}') for label,n in [('24 ч.',1),('7 дней',7),('30 дней',30)]],[('90 дней','admin:x:report:90'),('Всё время','admin:x:report:0')],[('📤 Excel и CSV',f'admin:x:exports:{days}')],[('🧾 Добавить расход','admin:x:input:expense')],[('← Назад','admin:commerce')]]
        from aiogram.types import (RichMessage,RichBlockSectionHeading,RichBlockParagraph,RichBlockTable,
            RichBlockTableCell,RichBlockDetails)
        def cell(value,header=False,align='left'):
            return RichBlockTableCell(align=align,valign='middle',text=str(value),is_header=header)
        money_cells=[[cell('Показатель',True),cell('Сумма',True,'right')],
            [cell('Выручка'),cell(money(revenue),False,'right')],[cell('Возвраты'),cell(money(refunds),False,'right')],
            [cell('Чистая выручка'),cell(money(revenue-refunds),False,'right')],[cell('Средний чек'),cell(money(sum(item.amount for item in sales)//len(sales) if sales else 0),False,'right')],
            [cell('Расходы'),cell(money(costs),False,'right')],[cell('Реферальные начисления'),cell(money(rewards),False,'right')],
            [cell('Результат до налогов'),cell(money(revenue-refunds-costs-rewards),False,'right')]]
        tariff_cells=[[cell('Тариф',True),cell('Продаж',True,'right'),cell('Покупателей',True,'right'),cell('Чистая выручка',True,'right')]]
        for name,n,total,buyers,returned in sorted(groups.values(),key=lambda g:g[1],reverse=True):
            tariff_cells.append([cell(name),cell(n,False,'right'),cell(len(buyers),False,'right'),cell(money(total-returned),False,'right')])
        if len(tariff_cells)==1: tariff_cells.append([cell('Продаж пока нет'),cell('0',False,'right'),cell('0',False,'right'),cell(money(0),False,'right')])
        rich=RichMessage(blocks=[
            RichBlockSectionHeading(text=f'Отчёт о продажах · {label}',size=2),
            RichBlockSectionHeading(text='Деньги',size=3),
            RichBlockTable(cells=money_cells,is_bordered=True,is_striped=True,is_compact=True),
            RichBlockSectionHeading(text='По тарифам',size=3),
            RichBlockTable(cells=tariff_cells,is_bordered=True,is_striped=True,is_compact=True),
            RichBlockDetails(summary='Заказы, клиенты и выплаты',blocks=[RichBlockParagraph(text=(
                f'Продаж: {len(sales)} · уникальных покупателей: {len({p.user_id for p in sales})}\n'
                f'Новых клиентов: {await count(User)} · выдано подписок: {await count(Subscription)} · активных сейчас: {active}\n'
                f'Пополнений ЮKassa: {money(topups)} · покупки с баланса: {money(wallet)}\n'
                f'Выплачено: {money(paidout)} · резерв на вывод: {money(reserved)}'))]),
        ])
        return Screen(text,rows,rich=rich)
