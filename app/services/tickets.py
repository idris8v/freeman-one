import json,secrets
from html import escape
from sqlalchemy import select, update, func, or_
from app.database.models import Ticket, TicketMessage, User, ChatNotice, Activity, ForumTask, now
from app.services.screens import Screen
from app.services.activity import record

STATUSES = {'waiting_admin':'Не решено · ждёт поддержки','waiting_client':'Ждёт ответа клиента','in_progress':'В работе','needs_info':'Нужны данные клиента','closed':'Решено'}


class TicketService:
    def __init__(self, repo, settings):
        self.repo, self.settings, self.session = repo, settings, repo.session

    def admin(self, actor):
        if not self.settings.can_admin_action(actor.telegram_id, 'admin:t:home'):
            raise PermissionError('Admin access denied')

    async def ticket(self, id, actor, admin=False):
        if admin:
            self.admin(actor)
        item = await self.session.get(Ticket, int(id))
        if not item or (not admin and item.user_id != actor.id):
            raise ValueError('Обращение недоступно.')
        return item


    async def delete(self, ticket_id, actor):
        """Delete the shared ticket and dependent records for both website roles."""
        from sqlalchemy import delete
        from app.database.models import WebSupportAttachment, AIConversation, AIJob, AIMessage, AdminAudit
        self.admin(actor)
        item=await self.session.get(Ticket,int(ticket_id),with_for_update=True)
        if not item:raise ValueError('Обращение недоступно.')
        if await self.repo.setting(f'support_ticket:{item.user_id}','')==str(item.id):
            await self.repo.set_setting(f'support_ticket:{item.user_id}','0')
            await self.repo.set_setting(f'support_dialog:{item.user_id}','0')
        convs=list(await self.session.scalars(select(AIConversation).where(AIConversation.ticket_id==item.id).with_for_update()))
        for conv in convs:
            conv.ticket_id=None;conv.mode='closed';conv.generation+=1
            await self.session.execute(update(AIJob).where(AIJob.message_id.in_(select(AIMessage.id).where(AIMessage.conversation_id==conv.id)),AIJob.status.in_(('pending','processing'))).values(status='canceled'))
        await self.session.execute(delete(ChatNotice).where(ChatNotice.ticket_id==item.id))
        await self.session.execute(delete(ForumTask).where(ForumTask.ticket_id==item.id))
        await self.session.execute(delete(WebSupportAttachment).where(WebSupportAttachment.message_id.in_(select(TicketMessage.id).where(TicketMessage.ticket_id==item.id))))
        await self.session.execute(delete(TicketMessage).where(TicketMessage.ticket_id==item.id))
        await self.session.flush()
        self.session.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='ticket_delete',target=str(item.id),details='Обращение и связанные сообщения удалены администратором'))
        await self.session.delete(item)

    async def select_dialog(self, actor, item):
        if item.user_id != actor.id or item.status == 'closed':
            raise ValueError('Обращение закрыто или недоступно.')
        await self.repo.set_setting(f'support_ticket:{actor.id}', str(item.id))
        await self.repo.set_setting(f'support_dialog:{actor.id}', '1')

    async def client_reply_ticket(self, actor, chat_id, message_id):
        # Reply-to is authoritative; never route a reply to another/latest ticket.
        if not message_id or chat_id != actor.telegram_id or chat_id <= 0:
            return None
        return await self.session.scalar(select(Ticket).join(ChatNotice, ChatNotice.ticket_id == Ticket.id).where(
            Ticket.user_id == actor.id, ChatNotice.target_id == chat_id, ChatNotice.kind == 'client',
            or_(ChatNotice.message_id == message_id, ChatNotice.media_message_id == message_id)))

    async def current_dialog(self, actor):
        selected = await self.repo.setting(f'support_ticket:{actor.id}', '')
        if selected.isdigit() and int(selected):
            item = await self.session.get(Ticket, int(selected))
            if item and item.user_id == actor.id and item.status != 'closed':
                return item
        items = list(await self.session.scalars(select(Ticket).where(
            Ticket.user_id == actor.id, Ticket.status != 'closed').order_by(Ticket.updated_at.desc()).limit(2)))
        if len(items) > 1:
            raise ValueError('У вас несколько открытых обращений. Выберите нужное в «Мои обращения» → «Добавить сообщение».')
        return items[0] if items else None

    async def finish_dialog(self, item, event_id):
        selected = await self.repo.setting(f'support_ticket:{item.user_id}', '')
        if selected not in ('', '0', str(item.id)):
            return  # Closing an older ticket must not interrupt a different conversation.
        await self.repo.set_setting(f'support_dialog:{item.user_id}', '0')
        await self.repo.set_setting(f'support_ticket:{item.user_id}', '0')
        user = await self.session.get(User, item.user_id)
        if user and user.telegram_id and user.telegram_id > 0:
            await self.repo.add(ChatNotice(dedup=f'ticket.home:{event_id}', target_id=user.telegram_id,
                kind='support_home', ticket_id=item.id, text='', buttons='[]'))

    async def set_status(self,id,actor,status,admin=False):
        import secrets
        if status not in STATUSES:raise ValueError('Неизвестный статус обращения.')
        item=await self.ticket(id,actor,admin)
        await self.session.execute(update(Ticket).where(Ticket.id==item.id).values(updated_at=Ticket.updated_at))
        await self.session.refresh(item)
        if not admin and status!='closed' and not (status=='waiting_admin' and item.status=='closed'):
            raise PermissionError('Клиент может решить вопрос или повторно открыть его.')
        if item.status==status:
            from app.services.freeman_ai.service import ticket_status
            await ticket_status(self.session,self.settings,item)
            return item
        item.status=status;item.updated_at=now()
        from app.services.freeman_ai.service import ticket_status
        await ticket_status(self.session,self.settings,item)
        text='Статус обращения: '+STATUSES[status]
        event=await self.repo.add(TicketMessage(ticket_id=item.id,role='system',text=text,source='status:'+secrets.token_hex(24)))
        if status=='closed':
            task=await self.session.scalar(select(ForumTask).where(ForumTask.ticket_id==item.id))
            if task:task.status='pending'
            else:await self.repo.add(ForumTask(ticket_id=item.id))
        user=await self.session.get(User,item.user_id)
        targets=([user.telegram_id] if user.telegram_id and user.telegram_id > 0 else []) if admin else []
        if not admin:
            group=await self.repo.setting('support_chat_id')
            if group:targets=[]
        for target in targets:
            await self.repo.add(ChatNotice(dedup=f'ticket.status:{event.id}:{target}',target_id=target,kind='client' if admin else 'support',ticket_id=item.id,text=text,buttons='[]'))
        if status=='closed':await self.finish_dialog(item,event.id)
        return item

    async def bind(self, actor, kind, chat_id):
        if not self.settings.is_owner(actor.telegram_id):
            raise PermissionError('Только владелец привязывает служебные чаты.')
        if kind not in ('support', 'history') or chat_id >= 0:
            raise ValueError('Привязка доступна в группе или супергруппе.')
        other = 'history' if kind == 'support' else 'support'
        if await self.repo.setting(other+'_chat_id') == str(chat_id):
            raise ValueError('Для поддержки и истории нужны разные группы.')
        if kind=='history':
            from app.services.admin_notifications import bind
            await bind(self.repo,chat_id)
        else:await self.repo.set_setting(kind+'_chat_id',str(chat_id))
        # Pending notices follow the new destination; sent messages remain in their original chat.
        await self.session.execute(update(ChatNotice).where(ChatNotice.kind == kind, ChatNotice.status.in_(['pending','failed'])).values(target_id=chat_id))
        if kind=='support':
            await self.session.execute(update(ForumTask).where(ForumTask.status=='failed').values(status='pending'))
            items=list(await self.session.scalars(select(Ticket).where(Ticket.status!='closed').order_by(Ticket.id.desc())))
            users=set()
            for item in items:
                if item.user_id in users:
                    continue
                users.add(item.user_id)
                key=f'forum.bootstrap:{chat_id}:{item.user_id}'
                if not await self.session.scalar(select(ChatNotice.id).where(ChatNotice.dedup==key)):
                    await self.repo.add(ChatNotice(dedup=key,target_id=chat_id,kind='support',ticket_id=item.id,
                        text='🛟 Эта тема закреплена за клиентом. Новые сообщения и ответы будут здесь; предыдущая переписка сохранена в админке.'))
        return '✅ Чат '+('техподдержки' if kind=='support' else 'истории событий')+' привязан. Команду может выполнить только владелец бота.'

    def mini_screen(self,actor,mode='list'):
        from app.services.web_login import issue_direct
        from urllib.parse import urlencode
        token=issue_direct(self.settings,actor,purpose='direct_support:'+mode)
        target='webapp:'+self.settings.web_base_url+'/auth/bot/support?'+urlencode({'mode':mode,'token':token})
        return Screen('💬 <b>Поддержка в Mini App</b>\n\nПереписка, ответы и управление обращениями доступны на сайте и внутри Telegram.',[[('💬 Открыть поддержку',target)],[('← Назад','admin:home' if mode=='staff' else 'help')]],structured=True)

    async def render(self, action, actor):
        p=action.split(':')
        admin=p[0]=='admin'
        if admin:
            self.admin(actor)
            p=p[2:]
        else:
            p=p[1:]
        page=p[0]
        if admin:return self.mini_screen(actor,'staff')
        back=[('← Назад','admin:t:home' if admin else 't:list:0' if page in ('ticket','closeconfirm','close','message') else 'help')]
        if admin and page in ('deleteconfirm','delete'):
            item=await self.ticket(p[1],actor,True)
            if page=='deleteconfirm':
                token=secrets.token_hex(8)
                await self.repo.set_setting('ticket_delete:'+token,json.dumps(dict(actor=actor.telegram_id,ticket=item.id,expires=now().timestamp()+300)))
                return Screen(f'Удалить обращение #{item.id}?\n\nПереписка и вложения исчезнут из базы. Действие нельзя отменить.',[[('Удалить обращение',f'admin:t:delete:{item.id}:{token}')],[('Отмена',f'admin:t:ticket:{item.id}:0')]])
            from app.database.models import AppSetting
            from sqlalchemy import delete
            raw=await self.repo.setting('ticket_delete:'+p[2]);data=json.loads(raw or '{}')
            if data.get('actor')!=actor.telegram_id or data.get('ticket')!=item.id or data.get('expires',0)<now().timestamp():raise ValueError('Подтверждение недоступно.')
            claim=await self.session.execute(delete(AppSetting).where(AppSetting.key=='ticket_delete:'+p[2],AppSetting.value==raw))
            if claim.rowcount!=1:raise ValueError('Подтверждение уже использовано.')
            await self.delete(item.id,actor)
            return Screen('Обращение удалено.',[[('Все обращения','admin:t:list:all:0')]])
        if admin and page=='guide':
            from app.services.guides import guide
            return guide('support')
        if admin and page=='topicclose':
            item=await self.session.scalar(select(Ticket).where(Ticket.user_id==int(p[1]),Ticket.status!='closed').order_by(Ticket.id.desc()).limit(1))
            if not item:
                return Screen('Открытых обращений нет.',[back])
            return await self.render(f'admin:t:closeconfirm:{item.id}',actor)
        if page=='message':
            message=await self.session.get(TicketMessage,int(p[1]))
            if not message:
                raise ValueError('Сообщение недоступно.')
            await self.ticket(message.ticket_id,actor,admin)
            prefix='admin:t' if admin else 't'
            return Screen(f'<b>{"Клиент" if message.role=="client" else "Система" if message.role=="system" else "Поддержка"} · {message.created_at:%d.%m.%Y %H:%M} UTC</b>\n\n'+escape(message.text or 'Вложение'),
                [[('← Назад',f'{prefix}:ticket:{message.ticket_id}:0')]],photo=message.photo,document=message.document)
        if admin and page=='home':
            waiting=await self.session.scalar(select(func.count()).select_from(Ticket).where(Ticket.status=='waiting_admin'))
            return Screen(f'🛟 <b>Техподдержка</b>\n\nЖдут ответа: {waiting}',
                [[('📬 Открытые тикеты','admin:t:list:open:0')],[('🗂 Все тикеты','admin:t:list:all:0')],
                 [('🔗 Чаты поддержки и истории','admin:t:chats')],[('🗓 История событий','admin:t:history:0')],[('← Назад','admin:home')]])
        if admin and page in ('historyprefs','historytoggle'):
            from app.services.activity import HISTORY_CATEGORIES
            if page=='historytoggle':
                key=p[1]
                if key not in HISTORY_CATEGORIES:
                    raise ValueError('Категория недоступна.')
                field='history_category:'+key
                value=await self.repo.setting(field,HISTORY_CATEGORIES[key][1])
                await self.repo.set_setting(field,'0' if value=='1' else '1')
            rows=[]
            for key,(name,default) in HISTORY_CATEGORIES.items():
                enabled=await self.repo.setting('history_category:'+key,default)=='1'
                rows.append([(('✅ ' if enabled else '⬜ ')+name,f'admin:t:historytoggle:{key}')])
            return Screen('🗞 <b>Что отправлять в группу событий</b>\n\nПо умолчанию только важные события: регистрации, платные подписки, покупки и выплаты. Тексты поддержки, кнопки и служебные подсказки туда не отправляются. Полная история остаётся в базе. Настройки влияют на будущую отправку; старые исключённые события не рассылаются при включении категории.',rows+[[('← Назад','admin:t:history:0')]])
        if admin and page=='chats':
            support=await self.repo.setting('support_chat_id') or 'не привязан (обращения идут администратору в личный чат)'
            history=await self.repo.setting('history_chat_id') or 'не привязан (события сохраняются в базе)'
            failed=await self.session.scalar(select(func.count()).select_from(ChatNotice).where(ChatNotice.status=='failed'))
            return Screen(f'🔗 <b>Отдельные чаты</b>\n\nПоддержка: {escape(support)}\nИстория: {escape(history)}\nОшибок доставки: {failed}\n\nСоздайте две закрытые группы и добавьте @{self.settings.bot_username}.\nВ группе поддержки отправьте /bind_support\nВ группе истории отправьте /bind_history\n\nНужны права бота на отправку сообщений. Для ответа используйте кнопку «Ответить» или ответ на сообщение бота. Privacy Mode отключать не требуется.\nКоманды привязки и работа с тикетами доступны только вашему Telegram ID.', [[('📬 Ошибки доставки','admin:t:failed:0')],back])
        if admin and page=='failed':
            offset=max(0,int(p[1]))
            jobs=list(await self.session.scalars(select(ChatNotice).where(ChatNotice.status=='failed').order_by(ChatNotice.id.desc()).offset(offset).limit(6)))
            rows=[[(f'#{job.id} · {job.kind} · чат {job.target_id}',f'admin:t:retryconfirm:{job.id}')] for job in jobs[:5]]
            if offset:
                rows.append([('← Предыдущие',f'admin:t:failed:{max(0,offset-5)}')])
            if len(jobs)>5:
                rows.append([('Следующие →',f'admin:t:failed:{offset+5}')])
            return Screen('Ошибки доставки. Проверьте, что бот добавлен в группу и может писать, а клиент не заблокировал бота. Затем повторите доставку.' if jobs else 'Ошибок доставки нет.',rows+[[('← Назад','admin:t:chats')]])
        if admin and page in ('retryconfirm','retry'):
            job=await self.session.get(ChatNotice,int(p[1]))
            if not job:
                raise ValueError('Сообщение не найдено.')
            if page=='retry':
                await self.session.execute(update(ChatNotice).where(ChatNotice.id==job.id,ChatNotice.status=='failed').values(status='pending',attempts=0,available_at=now()))
                await self.session.refresh(job)
            rows=[[('✅ Повторить доставку',f'admin:t:retry:{job.id}')]] if job.status=='failed' else []
            return Screen(f'Сообщение #{job.id}\nЧат: {job.target_id}\nСтатус: {job.status}\nТикет: {job.ticket_id or "—"}\n\nПовторить после устранения причины ошибки?',rows+[[('← Назад','admin:t:failed:0')]])
        if admin and page=='history':
            offset=max(0,int(p[1]))
            items=list(await self.session.scalars(select(Activity).order_by(Activity.id.desc()).offset(offset).limit(6)))
            text='\n\n'.join(f'#{item.id} · {item.created_at:%d.%m.%Y %H:%M} UTC\n{escape(item.text[:450])}' for item in items[:5]) or 'Событий пока нет.'
            rows=[]
            if offset:
                rows.append([('← Предыдущие',f'admin:t:history:{max(0,offset-5)}')])
            if len(items)>5:
                rows.append([('Следующие →',f'admin:t:history:{offset+5}')])
            return Screen('🗓 <b>История событий</b>\n\n'+text,rows+[[('⚙️ Фильтр уведомлений','admin:t:historyprefs')],back])
        if page=='transcript':
            item=await self.ticket(p[1],actor,admin)
            from app.services.ticket_transcript import render_html
            messages=list(await self.session.scalars(select(TicketMessage).where(TicketMessage.ticket_id==item.id).order_by(TicketMessage.id)))
            prefix='admin:t' if admin else 't'
            return Screen('🗂 Переписка готова. Скачайте HTML-файл и откройте его в браузере. '
                          'Файл содержит личную переписку — храните его у себя.',
                          [[('← К обращению',f'{prefix}:ticket:{item.id}:0')]],
                          document=(render_html(item,messages,STATUSES.get(item.status,item.status)),f'Freeman-support-{item.id}.html'))
        if page=='list':
            mode=p[1] if admin else 'all'
            if mode not in ('open','all'):
                raise ValueError('Список недоступен.')
            offset=max(0,int(p[2] if admin else p[1]))
            query=select(Ticket)
            if not admin:
                query=query.where(Ticket.user_id==actor.id)
            if mode=='open':
                query=query.where(Ticket.status!='closed')
            items=list(await self.session.scalars(query.order_by(Ticket.updated_at.desc(),Ticket.id.desc()).offset(offset).limit(6)))
            prefix='admin:t' if admin else 't'
            rows=[[(f'#{item.id} · {STATUSES[item.status]}',f'{prefix}:ticket:{item.id}:0')] for item in items[:5]]
            path=f'{prefix}:list:{mode}' if admin else f'{prefix}:list'
            if offset:
                rows.append([('← Предыдущие',f'{path}:{max(0,offset-5)}')])
            if len(items)>5:
                rows.append([('Следующие →',f'{path}:{offset+5}')])
            return Screen('🗂 <b>Мои обращения</b>\n\nВыберите обращение, чтобы прочитать ответы или скачать полный HTML-чат.' if items else '🗂 <b>Мои обращения</b>\n\nОбращений пока нет. Если нужна помощь, напишите в поддержку.',rows+([] if items or admin else [[('💬 Написать в поддержку','t:input:new')]])+[back],structured=True)
        if page in ('ticket','closeconfirm','close'):
            item=await self.ticket(p[1],actor,admin)
            if page=='close':
                await self.set_status(item.id,actor,'closed',admin)
            user=await self.session.get(User,item.user_id)
            offset=max(0,int(p[2])) if page=='ticket' and len(p)>2 else 0
            messages=list(await self.session.scalars(select(TicketMessage).where(TicketMessage.ticket_id==item.id).order_by(TicketMessage.id.desc()).offset(offset).limit(5)))
            text=f'🛟 <b>Обращение #{item.id}</b>\n\nСтатус: {STATUSES[item.status]}\n'
            if admin:
                text+=f'Клиент: <code>{user.telegram_id}</code> · {escape(user.display_name)}\n'
            for message in reversed(messages[:4]):
                role='Клиент' if message.role=='client' else 'Система' if message.role=='system' else 'Поддержка'
                text+=f'\n\n<b>{role} · {message.created_at:%d.%m %H:%M} UTC</b>\n{escape(message.text[:600])}'+('…' if len(message.text)>600 else '')
                if message.photo or message.document:
                    text+='\n📎 Вложение'
            prefix='admin:t' if admin else 't'
            rows=[[(f'📩 Сообщение #{message.id} целиком',f'{prefix}:message:{message.id}')] for message in reversed(messages[:4])]
            if offset:
                rows.append([('← Новее',f'{prefix}:ticket:{item.id}:{max(0,offset-4)}')])
            if len(messages)>4:
                rows.append([('Старые сообщения →',f'{prefix}:ticket:{item.id}:{offset+4}')])
            if item.status!='closed':
                rows.append([('✍️ Ответить' if admin else '✍️ Добавить сообщение',f'{prefix}:input:reply:{item.id}')])
                if page=='closeconfirm':
                    text+='\n\nЗакрыть обращение?'
                    rows.append([('✅ Закрыть',f'{prefix}:close:{item.id}')])
                else:
                    rows.append([('✅ Закрыть обращение',f'{prefix}:closeconfirm:{item.id}')])
            if admin and user.telegram_id:
                rows.append([('💬 Связаться с клиентом',f'url:tg://user?id={user.telegram_id}')])
            if admin:rows.append([('🗑 Удалить обращение',f'admin:t:deleteconfirm:{item.id}')])
            if not admin:
                from app.services.web_login import issue_direct
                from urllib.parse import urlencode
                token=issue_direct(self.settings,actor,purpose=f'direct_chat:{item.id}')
                target='webapp:'+self.settings.web_base_url+'/auth/bot/chat?'+urlencode({'ticket':item.id,'token':token})
                text=f'💬 <b>Обращение №{item.id}</b>\n\nСтатус: {STATUSES[item.status]}\n\nОткройте чат внутри Telegram, чтобы прочитать переписку и ответы поддержки. Сообщения с сайта тоже доступны здесь.'
                rows=[[('💬 Открыть чат',target)],[('← Назад','t:list:0')]]
            else:
                rows+=[[('🌐 Скачать чат для браузера',f'{prefix}:transcript:{item.id}')],[('🔄 Обновить',f'{prefix}:ticket:{item.id}:0')],back]
            return Screen(text,rows,structured=True)
        if page=='input':
            if p[1]=='reply':
                item=await self.ticket(p[2],actor,admin)
                if item.status=='closed':
                    raise ValueError('Обращение закрыто. Создайте новое.')
                if not admin:await self.select_dialog(actor,item)
            elif p[1]!='new' or admin:
                raise ValueError('Действие недоступно.')
            if not admin and p[1]=='new':
                item=await self.current_dialog(actor)
                if item:await self.select_dialog(actor,item)
                else:await self.repo.set_setting(f'support_dialog:{actor.id}','1')
            prompt = 'Введите ответ клиенту (до 3500 символов). Можно приложить фотографию или файл. Ответ будет отправлен клиенту от имени бота.' if admin else 'Вы в чате поддержки. Просто пишите сообщения, отправляйте фото или файлы. Ответы придут сюда без дополнительных кнопок. /start — вернуться в меню. До 3500 символов в сообщении.'
            return Screen(prompt,[back])
        raise ValueError('Раздел недоступен.')

    async def input(self,action,actor,text,source,photo=None,document=None,*,force_new=False):
        p=action.split(':')
        admin=p[0]=='admin'
        if admin:
            self.admin(actor)
            p=p[3:]
        else:
            p=p[2:]
        if p[0] not in ('new','reply') or (admin and p[0]!='reply'):
            raise ValueError('Действие недоступно.')
        text=text.strip()
        if (not text and not photo and not document) or len(text)>3500 or (photo and document):
            raise ValueError('Пришлите текст до 3500 символов, фотографию или файл.')
        existing=await self.session.scalar(select(TicketMessage).where(TicketMessage.source==source))
        if existing:
            await self.ticket(existing.ticket_id,actor,admin)
            return await self.render(f'{"admin:t" if admin else "t"}:ticket:{existing.ticket_id}:0',actor)
        if p[0]=='new':
            await self.session.execute(update(User).where(User.id==actor.id).values(status=User.status))
            existing=await self.session.scalar(select(TicketMessage).where(TicketMessage.source==source))
            if existing:
                return await self.render(f't:ticket:{existing.ticket_id}:0',actor)
            item=None if force_new else await self.current_dialog(actor)
            if not item:
                item=await self.repo.add(Ticket(user_id=actor.id))
                await record(self.repo,f'ticket.created:{item.id}',f'🛟 Новое обращение #{item.id}\nКлиент: {actor.telegram_id} · {actor.display_name}')
        else:
            item=await self.ticket(p[1],actor,admin)
        # Lock the ticket before inspecting its status; close/reply races cannot reopen it.
        await self.session.execute(update(Ticket).where(Ticket.id==item.id).values(updated_at=Ticket.updated_at))
        await self.session.refresh(item)
        existing=await self.session.scalar(select(TicketMessage).where(TicketMessage.source==source))
        if existing:
            return await self.render(f'{"admin:t" if admin else "t"}:ticket:{item.id}:0',actor)
        if item.status=='closed':
            raise ValueError('Обращение закрыто. Создайте новое.')
        item.status='waiting_client' if admin else 'waiting_admin'
        item.updated_at=now()
        message=await self.repo.add(TicketMessage(ticket_id=item.id,role='admin' if admin else 'client',text=text,photo=photo,document=document,source=source))
        user=await self.session.get(User,item.user_id)
        from app.database.models import AIConversation
        ai_linked=await self.session.scalar(select(AIConversation).where(AIConversation.ticket_id==item.id,AIConversation.mode!='closed').order_by(AIConversation.id.desc()).limit(1))
        if not ai_linked and not admin:
            from app.services.freeman_ai.service import attach_ticket
            ai_linked=await attach_ticket(self.session,self.settings,item,source)
        if ai_linked:
            from app.services.freeman_ai.service import switch_mode,append
            if not admin and (photo or document):
                from app.services.freeman_ai.service import handoff
                await handoff(self.session,self.settings,ai_linked,'attachment_requires_specialist')
            if admin:await switch_mode(self.session,self.settings,ai_linked,'human')
            await append(self.session,self.settings,ai_linked,text or 'Пришло вложение. Откройте обращение в разделе поддержки.','ticket:'+str(message.id),'operator' if admin else 'client',mirror_ticket=False)
        if admin:
            targets=[user.telegram_id] if user.telegram_id and user.telegram_id > 0 and not (ai_linked and ai_linked.channel=='telegram') else []
            kind='client'
            body=text or ''
            rows=[]
            await record(self.repo,f'ticket.reply:{message.id}',f'🛟 Ответ поддержки в тикете #{item.id}\nКлиент: {user.telegram_id}')
        else:
            await self.repo.set_setting(f'support_dialog:{actor.id}','0')
            target=await self.repo.setting('support_chat_id')
            targets=[]
            kind='support'
            body=f'🛟 Тикет #{item.id} · сообщение клиента\nКлиент: {user.telegram_id} · {user.display_name}\nUsername: @{user.username}' if user.username else f'🛟 Тикет #{item.id} · сообщение клиента\nКлиент: {user.telegram_id} · {user.display_name}'
            body+='\n\n'+(text or 'Вложение')
            rows=[[['✍️ Ответить',f'admin:t:input:reply:{item.id}'],['🛟 Тикет',f'admin:t:ticket:{item.id}:0']],[['✅ Закрыть',f'admin:t:closeconfirm:{item.id}']]]
        for target in targets:
            await self.repo.add(ChatNotice(dedup=f'ticket.message:{message.id}:{target}',target_id=target,kind=kind,ticket_id=item.id,text=body,photo=photo,document=document,buttons=json.dumps(rows,ensure_ascii=False)))
        if not admin and await self.repo.setting('web_support_autoreply_enabled','0')=='1':
            auto_text=(await self.repo.setting('web_support_autoreply_text','Спасибо! Мы получили сообщение и скоро ответим.')).strip()
            if auto_text:
                auto=await self.repo.add(TicketMessage(ticket_id=item.id,role='admin',text=auto_text,source=f'auto:{message.id}'))
                if user.telegram_id and user.telegram_id > 0:await self.repo.add(ChatNotice(dedup=f'ticket.auto:{auto.id}',target_id=user.telegram_id,kind='client',ticket_id=item.id,text=auto_text,buttons='[]'))
        screen=await self.render(f'{"admin:t" if admin else "t"}:ticket:{item.id}:0',actor)
        screen.text='✅ '+('Ответ поставлен в очередь клиенту.' if admin else 'Сообщение отправлено в поддержку.')+'\n\n'+screen.text
        return screen

    async def replied_ticket(self,chat_id,message_id,actor):
        self.admin(actor)
        return await self.session.scalar(select(ChatNotice.ticket_id).where(ChatNotice.target_id==chat_id,
            ChatNotice.kind=='support',or_(ChatNotice.message_id==message_id,ChatNotice.media_message_id==message_id)))
