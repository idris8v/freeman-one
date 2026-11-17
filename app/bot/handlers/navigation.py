import asyncio
from aiogram import F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, LinkPreviewOptions, InputMediaPhoto, InputMediaAnimation, InputMediaVideo, InputMediaDocument, InputRichMessage, BufferedInputFile, ForceReply, ReactionTypeEmoji, ReplyKeyboardRemove, PreCheckoutQuery
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from aiogram.fsm.context import FSMContext
from app.bot.states import PromoInput, AdminInput, SubscriptionNameInput, ExtendedInput, TicketInput, GiftInput, TopupInput
from app.bot.keyboards import keyboard


async def acknowledge_callback(query):
    """A slow callback ACK must not delay the requested screen."""
    try:
        await asyncio.wait_for(query.answer(), timeout=3)
    except (TelegramAPIError, TimeoutError):
        pass

_callback_ack_tasks = set()

def start_callback_ack(query):
    task = asyncio.create_task(acknowledge_callback(query))
    _callback_ack_tasks.add(task)
    task.add_done_callback(_callback_ack_tasks.discard)
    return task


def stale_media(error):
    message = str(error).lower()
    return 'wrong file identifier' in message or 'failed to get http url content' in message or 'file_id' in message


def input_rich(value):
    """Convert Telegram's received RichMessage model to its sendable model."""
    if isinstance(value, InputRichMessage):
        return value
    return InputRichMessage.model_validate(value.model_dump(exclude_none=True))


def navigation_rich(screen):
    """Keep navigation editable even when the screen contains a QR image."""
    from aiogram.types import InputRichMessageMedia
    from app.bot.display_text import clean
    clean(screen)
    if screen.native_text:return None
    if screen.rich:return input_rich(screen.rich)
    media=[];html=[]
    def add(kind,value):
        if isinstance(value,tuple):value=BufferedInputFile(value[0],filename=value[1])
        media_id='media'+str(len(media))
        cls={'photo':InputMediaPhoto,'animation':InputMediaAnimation,'video':InputMediaVideo}[kind]
        media.append(InputRichMessageMedia(id=media_id,media=cls(media=value)))
        tag='img' if kind=='photo' else 'video'
        uri='photo' if kind=='photo' else 'video'
        html.append(f'<{tag} src="tg://{uri}?id={media_id}"></{tag}>' if tag!='img' else f'<img src="tg://photo?id={media_id}">')
    if screen.banner:add(*screen.banner)
    if screen.photo:add('photo',screen.photo)
    elif screen.animation:add('animation',screen.animation)
    elif screen.video:add('video',screen.video)
    if '<code>' in screen.text or '<tg-emoji' in screen.text or screen.structured:
        from app.bot.rich_copy import copyable_rich
        return copyable_rich(screen,media)
    if not media:return None
    # Rich HTML collapses literal newlines; use explicit breaks outside preformatted blocks.
    import re
    text=''.join(part if part.startswith('<pre') else part.replace('\n','<br>') for part in re.split(r'(<pre\b[^>]*>.*?</pre>)',screen.text,flags=re.S))
    # Use the documented nested pre/code structure for mobile rich clients.
    text=re.sub(r'<pre>(?!<code>)(.*?)</pre>',r'<pre><code>\1</code></pre>',text,flags=re.S)
    text=re.sub(r'(?:<br>)+(?=<pre>)','',text)
    text=re.sub(r'(?<=</pre>)(?:<br>)+','',text)
    html.append(text)
    return InputRichMessage(html='\n'.join(html),media=media)


async def show(event, screen):
    from app.bot.display_text import clean
    clean(screen)
    from app.bot.delivery import edit_unavailable, fresh_context
    import logging
    if isinstance(event, CallbackQuery) and not isinstance(event.message, Message):
        logging.getLogger(__name__).info('Bot navigation recovery: inaccessible_message')
        return await _show(fresh_context(event), screen)
    try:
        return await _show(event, screen)
    except TelegramBadRequest as error:
        if not isinstance(event, CallbackQuery):
            raise
        if 'message is not modified' in str(error).lower():
            return
        if not edit_unavailable(error):
            raise
        logging.getLogger(__name__).info('Bot navigation recovery: edit_unavailable')
        # Only repeat presentation. Payments/provisioning/input handlers already ran.
        return await _show(fresh_context(event), screen)


async def _show(event, screen):
    from app.bot.latency import obsolete
    if obsolete():return
    if screen.avatar_telegram_id:
        from app.services.client_avatar import attach
        await attach(event.bot,screen)
    if isinstance(event,CallbackQuery):
        from app.services.support_counter import track
        track(event.message.chat.id,event.message.message_id,screen,event.from_user.id)
    options = dict(reply_markup=keyboard(screen.rows, screen.button_options), parse_mode='HTML', link_preview_options=LinkPreviewOptions(is_disabled=True))
    message = event.message if isinstance(event, CallbackQuery) else event
    if screen.native_text:
        # Native code entities, rather than rich code blocks, for tap-copy credentials.
        # Suppress configured home media only for this transport; keep admin settings intact.
        screen.photo=screen.animation=screen.video=screen.banner=screen.rich=None
    # Text and rich messages can be edited in either direction, without deleting them.
    # Explicit document downloads remain separate from the navigation message.
    if not screen.document and not screen.clear_keyboard:
        rich=navigation_rich(screen)
        if isinstance(event,CallbackQuery) and not (message.photo or message.animation or message.video or message.document):
            try:
                if rich:
                    await message.bot.edit_message_text(chat_id=message.chat.id,message_id=message.message_id,rich_message=rich,reply_markup=options['reply_markup'])
                else:await message.edit_text(screen.text,**options)
                return
            except TelegramBadRequest as error:
                if 'message is not modified' in str(error):return
                from app.bot.delivery import edit_unavailable
                if edit_unavailable(error):raise
                if rich:
                    # Unsupported rich content falls back to an editable text screen.
                    await message.edit_text(screen.text,**options);return
                raise
        if rich and not isinstance(event,CallbackQuery):
            try:
                result=await message.bot.send_rich_message(message.chat.id,rich,reply_markup=options['reply_markup'])
                from app.services.support_counter import track
                track(message.chat.id,result.message_id,screen,message.from_user.id)
                return
            except TelegramBadRequest:pass
        if isinstance(event,CallbackQuery) and (message.photo or message.animation or message.video or message.document) and not rich:
            # Preserve an existing legacy media message rather than delete/re-send it.
            await message.edit_caption(caption=screen.text,parse_mode='HTML',reply_markup=options['reply_markup']);return
    if screen.clear_keyboard:
        # Telegram forbids editing messages sent with ReplyKeyboardRemove.
        cleared=await message.answer('Клавиатура закрыта.',reply_markup=ReplyKeyboardRemove(),disable_notification=True)
        try:
            await cleared.as_(message.bot).delete()
        except TelegramBadRequest:
            pass
    if screen.banner:
        kind,file=screen.banner
        try:
            if kind=='photo':await message.answer_photo(file)
            elif kind=='animation':await message.answer_animation(file)
            elif kind=='video':await message.answer_video(file)
        except TelegramBadRequest as error:
            if not stale_media(error):raise
    if screen.rich:
        try:
            # Rich Message blocks provide the structured table/content, while the
            # ordinary inline keyboard remains the reliable navigation layer on
            # every supported Telegram client.
            await message.bot.send_rich_message(
                message.chat.id,
                input_rich(screen.rich),
                reply_markup=options['reply_markup'],
            )
            if isinstance(event,CallbackQuery):
                try: await message.delete()
                except TelegramBadRequest: pass
            return
        except TelegramBadRequest:
            # Older Telegram clients/accounts may reject a newly introduced
            # rich block. The ordinary screen remains fully usable.
            pass
    if screen.document:
        if isinstance(screen.document, str):
            file = screen.document
        else:
            data, name = screen.document
            file = BufferedInputFile(data, filename=name)
        if len(screen.text) <= 1024:
            if isinstance(event,CallbackQuery):
                try:
                    await message.edit_media(InputMediaDocument(media=file,caption=screen.text,parse_mode='HTML'),reply_markup=options['reply_markup'])
                    return
                except TelegramBadRequest as error:
                    if 'message is not modified' in str(error):
                        return
            try:
                await message.answer_document(file, caption=screen.text, reply_markup=options['reply_markup'], parse_mode='HTML')
            except TelegramBadRequest as error:
                if not stale_media(error):
                    raise
                await message.answer(screen.text, **options)
        else:
            try:
                await message.answer_document(file)
            except TelegramBadRequest as error:
                if not stale_media(error):
                    raise
            await message.answer(screen.text, **options)
        return
    if screen.animation:
        if len(screen.text)<=1024:
            if isinstance(event,CallbackQuery):
                try:
                    await message.edit_media(InputMediaAnimation(media=screen.animation,caption=screen.text,parse_mode='HTML'),reply_markup=options['reply_markup']);return
                except TelegramBadRequest as error:
                    if 'message is not modified' in str(error): return
            try:
                await message.answer_animation(screen.animation,caption=screen.text,parse_mode='HTML',reply_markup=options['reply_markup']);return
            except TelegramBadRequest as error:
                if not stale_media(error): raise
                await message.answer(screen.text,**options);return
        try:
            await message.answer_animation(screen.animation)
        except TelegramBadRequest as error:
            if not stale_media(error): raise
        await message.answer(screen.text,**options);return
    if screen.video:
        if len(screen.text)<=1024:
            if isinstance(event,CallbackQuery):
                try:
                    await message.edit_media(InputMediaVideo(media=screen.video,caption=screen.text,parse_mode='HTML'),reply_markup=options['reply_markup']);return
                except TelegramBadRequest as error:
                    if 'message is not modified' in str(error): return
            try:
                await message.answer_video(screen.video,caption=screen.text,parse_mode='HTML',reply_markup=options['reply_markup']);return
            except TelegramBadRequest as error:
                if not stale_media(error): raise
                await message.answer(screen.text,**options);return
        try:
            await message.answer_video(screen.video)
        except TelegramBadRequest as error:
            if not stale_media(error): raise
        await message.answer(screen.text,**options);return
    if screen.photo:
        photo=screen.photo if isinstance(screen.photo,str) else BufferedInputFile(screen.photo[0],filename=screen.photo[1])
        if len(screen.text) <= 1024:
            if isinstance(event, CallbackQuery):
                try:
                    await message.edit_media(InputMediaPhoto(media=photo, caption=screen.text, parse_mode='HTML'), reply_markup=options['reply_markup'])
                    return
                except TelegramBadRequest as error:
                    if 'message is not modified' in str(error):
                        return
            try:
                await message.answer_photo(photo, caption=screen.text, parse_mode='HTML', reply_markup=options['reply_markup'])
            except TelegramBadRequest as error:
                if not stale_media(error):
                    raise
                await message.answer(screen.text, **options)
            return
        # Long instructions/reports retain full text in a separate message.
        try:
            await message.answer_photo(photo)
        except TelegramBadRequest as error:
            if not stale_media(error):
                raise
        await message.answer(screen.text, **options)
        return
    if isinstance(event,CallbackQuery) and getattr(message,'rich_message',None):
        await message.answer(screen.text,**options)
        try: await message.delete()
        except TelegramBadRequest: pass
        return
    if isinstance(event, CallbackQuery) and (message.photo or message.animation or message.video or message.document):
        await message.answer(screen.text, **options)
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        return
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(screen.text, **options)
        except TelegramBadRequest as error:
            if 'message is not modified' in str(error):
                return
            if "message can't be edited" in str(error) or 'message to edit not found' in str(error):
                await event.message.answer(screen.text, **options)
            else:
                raise
    else:
        await event.answer(screen.text, **options)


def create_router():
    router = Router()

    @router.message(Command('ai', 'ai_stop', 'ai_link'))
    async def ai_command(message: Message, state: FSMContext, screens, user):
        if message.chat.type!='private':return
        from app.services.freeman_ai.service import channel_enabled,conversation
        from app.services.freeman_ai.identity import channel_identity,issue_challenge
        if not await channel_enabled(screens.repo.session,screens.settings,'telegram'):
            await message.answer('AI-помощник пока отключён. Поддержка доступна в меню.',parse_mode=None);return
        command=message.text.split()[0].split('@')[0]
        if command=='/ai_stop':
            await screens.repo.set_setting(f'ai_dialog:{user.id}','0')
            await message.answer('AI-диалог выключен. Возвращайтесь через /ai.',parse_mode=None);return
        await state.clear()
        identity=await channel_identity(screens.repo.session,screens.settings,'telegram',str(message.from_user.id))
        if command=='/ai_link':
            token=await issue_challenge(screens.repo.session,screens.settings,identity)
            await message.answer('Подтвердите аккаунт через обычный вход на сайте. Ссылка действует 10 минут:\n'+screens.settings.web_base_url.rstrip('/')+'/ai?link='+token,parse_mode=None);return
        await screens.repo.set_setting(f'ai_dialog:{user.id}','1')
        await conversation(screens.repo.session,screens.settings,identity=identity)
        await message.answer('Здравствуйте! Я Freeman AI. Помогу выбрать VPN-подписку или разобраться с подключением. Напишите вопрос. Для выхода — /ai_stop, для оператора — «оператор».',parse_mode=None)

    @router.message(Command('bind_support', 'bind_suppor', 'bind_history', 'bind_infra', 'bind_support_alerts'))
    async def bind_chat(message: Message, state: FSMContext, screens, user):
        await state.clear()
        if not screens.settings.is_owner(user.telegram_id):
            await message.answer('Этот раздел недоступен.')
            return
        from app.services.tickets import TicketService
        command = message.text.split('@')[0].split()[0]
        kind = 'support' if command in ('/bind_support','/bind_suppor') else 'history'
        try:
            chat_id = str(message.chat.id)
            if command == '/bind_support_alerts':
                from app.services.support_group import bind
                await bind(screens.repo, message.chat.id)
                await message.answer('✅ Группа уведомлений поддержки подключена.')
                return
            if command == '/bind_infra':
                if message.chat.type not in ('group', 'supergroup'):
                    raise ValueError('Отправьте /bind_infra в отдельной группе мониторинга.')
                if chat_id in (await screens.repo.setting('history_chat_id'), await screens.repo.setting('support_chat_id')):
                    raise ValueError('Для мониторинга нужна отдельная группа.')
                await screens.repo.set_setting('infra_chat_id', chat_id)
                await message.answer('Группа мониторинга подключена. Предупреждения о нагрузке и восстановлении будут приходить сюда.')
                return
            if chat_id == await screens.repo.setting('infra_chat_id'):
                raise ValueError('Эта группа уже используется для мониторинга.')
            if kind == 'support' and message.chat.id < 0:
                chat = await message.bot.get_chat(message.chat.id)
                member = await message.bot.get_chat_member(message.chat.id, message.bot.id)
                if not chat.is_forum:
                    raise ValueError('Включите «Темы» в настройках группы и повторите /bind_support.')
                if member.status != 'administrator' or not getattr(member, 'can_manage_topics', False):
                    raise ValueError('Назначьте бота администратором с правом «Управление темами», затем повторите /bind_support.')
            text = await TicketService(screens.repo, screens.settings).bind(user, kind, message.chat.id)
        except ValueError as error:
            text = str(error)
        except TelegramAPIError:
            text = 'Не удалось проверить группу. Убедитесь, что бот — администратор с правом «Управление темами», и повторите /bind_support.'
        await message.answer(text, parse_mode=None)

    @router.message(Command('close'))
    async def close_topic(message: Message, screens, user, group_context=False):
        if not group_context:
            return
        from app.services.forum import current_topic_ticket
        item = await current_topic_ticket(screens.repo, message.chat.id, message.message_thread_id)
        if not item:
            await message.answer('В этой теме нет открытого обращения.')
            return
        await confirm_topic_close(message, item)

    @router.message(F.forum_topic_closed)
    async def manual_topic_close(message: Message, screens, user, group_context=False):
        if not group_context:
            return
        from app.services.forum import current_topic_ticket
        from app.services.tickets import TicketService
        from app.database.models import ForumThread
        from sqlalchemy import update
        item=await current_topic_ticket(screens.repo,message.chat.id,message.message_thread_id)
        await screens.repo.session.execute(update(ForumThread).where(ForumThread.chat_id==message.chat.id,ForumThread.thread_id==message.message_thread_id).values(closed=True))
        if item:
            await TicketService(screens.repo,screens.settings).render(f'admin:t:close:{item.id}',user)

    @router.message(Command('tickets'))
    async def tickets(message: Message, state: FSMContext, screens, user):
        await state.clear()
        if not screens.settings.can_admin_action(user.telegram_id, 'admin:t:home'):
            await message.answer('Этот раздел недоступен.')
            return
        await show(message, await screens.render('admin:t:home', user))

    @router.message(Command('cancel'))
    async def cancel(message: Message, state: FSMContext, screens, user, group_context=False):
        await state.clear()
        if not group_context and await screens.repo.setting(f'support_dialog:{user.id}')=='1':
            await screens.repo.set_setting(f'support_dialog:{user.id}','0')
        screen=await screens.render('admin:t:home' if group_context else 'home', user)
        await show(message,screen)

    @router.message(Command('admin'))
    async def admin(message: Message, state: FSMContext, screens, user, group_context=False):
        await state.clear()
        if not screens.settings.is_admin(user.telegram_id):
            await message.answer('Этот раздел недоступен.')
            return
        await show(message, await screens.render('admin:t:home' if group_context else 'admin:home', user))

    @router.message(Command('paysupport'))
    async def payment_support(message: Message, state: FSMContext, screens, user, group_context=False):
        await state.clear()
        if group_context:
            return
        screen = await screens.render('support', user)
        screen.text = '💳 <b>Поддержка по оплате</b>\n\nОпишите проблему в новом обращении. Укажите способ оплаты, дату и сумму. Не присылайте полный номер карты, коды из SMS и пароли.\n\n' + screen.text
        await show(message, screen)

    @router.message(CommandStart())
    @router.message(Command('help'))
    async def start(message: Message, state: FSMContext, screens, user, group_context=False):
        await state.clear()
        if not group_context and await screens.repo.setting(f'support_dialog:{user.id}')=='1':
            await screens.repo.set_setting(f'support_dialog:{user.id}','0')
        page = 'help' if message.text.split('@')[0].split()[0] == '/help' else 'home'
        if not group_context and message.text.partition(' ')[2].startswith('auth_'):
            await show(message,await screens.render('tg_auth:'+message.text.partition(' ')[2][5:],user))
            return
        if not group_context and message.text.startswith('/start') and message.text.partition(' ')[2].startswith(('site_login','site_ref_','link_')):
            page = 'cabinet'
        if group_context:
            page = 'admin:t:home'
        elif message.text.startswith('/start') and message.text.partition(' ')[2].strip().startswith('ops_payout_'):
            payout_id = message.text.partition(' ')[2].strip().removeprefix('ops_payout_')
            if screens.settings.can_admin_action(user.telegram_id, 'admin:x:payout') and payout_id.isascii() and payout_id.isdecimal() and 0 < int(payout_id) < 2**63:
                try:
                    payout = await screens.render(f'admin:x:payout:{int(payout_id)}', user)
                except (ValueError, PermissionError):
                    await message.answer('Заявка недоступна.')
                else:
                    await show(message, payout)
                return
        elif message.text.startswith('/start') and message.text.partition(' ')[2].strip().startswith('ops_ticket_'):
            ticket_id = message.text.partition(' ')[2].strip().removeprefix('ops_ticket_')
            if not (screens.settings.can_admin_action(user.telegram_id, 'admin:t:home')
                    and ticket_id.isascii() and ticket_id.isdecimal() and 0 < int(ticket_id) < 2**63):
                await message.answer('Обращение недоступно.')
                return
            from app.services.tickets import TicketService
            try:
                ticket = await TicketService(screens.repo, screens.settings).render(f'admin:t:ticket:{int(ticket_id)}:0', user)
            except (ValueError, PermissionError):
                await message.answer('Обращение недоступно.')
            else:
                await show(message, ticket)
            return
        elif message.text.startswith('/start') and message.text.partition(' ')[2].strip().startswith('ops_client_'):
            customer_id = message.text.partition(' ')[2].strip().removeprefix('ops_client_')
            if screens.settings.can_admin_action(user.telegram_id, 'admin:client:0') and customer_id.isascii() and customer_id.isdecimal() and 0 < int(customer_id) < 2**63:
                await show(message, await screens.render(f'admin:client:{int(customer_id)}', user))
                return
        elif message.text.startswith('/start') and message.text.partition(' ')[2].strip() == 'free_proxy':
            from app.services.public_proxy import connect_screen, connection_url
            url = await connection_url(screens.repo)
            if url:
                await show(message, connect_screen(url))
                return
        elif message.text.startswith('/start') and message.text.partition(' ')[2].startswith('wb_'):
            from html import escape
            from app.services.screens import Screen
            try:
                payload=message.text.partition(' ')[2].split('_')
                if len(payload)!=4:raise ValueError('Неверная ссылка оплаты виджета.')
                screen=await screens.render('wpay:'+':'.join(payload[1:]),user)
            except (ValueError,PermissionError) as error:
                screen=Screen('Не удалось открыть оплату.\n\n'+escape(str(error)),[[('← Виджет','admin:widgetdemo')]],structured=True)
            await show(message,screen);return
        elif message.text.startswith('/start') and message.text.partition(' ')[2].startswith('fg_'):
            from app.services.bot_gift_links import BotGiftLinks,styled
            from html import escape
            try:screen=await BotGiftLinks(screens.repo,screens.settings).claim(message.text.partition(' ')[2][3:],user)
            except ValueError as error:
                screen=styled('🎁 <b>Подарок</b>\n\n'+escape(str(error))+'\n\nЕсли выдача ещё проверяется, откройте ту же ссылку повторно.',[[('💬 Помощь','help')]])
            await show(message,await screens.decorate('receipt',user,screen));return
        elif message.text.startswith('/start') and message.text.partition(' ')[2].strip().startswith('pc_'):
            from html import escape
            from app.services.admin_client_grant import rich
            from app.services.proxy_campaigns import CampaignRetry, ProxyCampaigns
            campaigns=ProxyCampaigns(screens.repo,screens.settings)
            token=message.text.partition(' ')[2].strip()[3:]
            from app.services.public_proxy import connect_screen, connection_url
            url = await connection_url(screens.repo)
            if url:
                from app.services.proxy_campaigns import campaign_key, status
                import json
                try:
                    raw = await screens.repo.setting(campaign_key(token))
                    if raw and status(json.loads(raw)) == 'Активна':
                        await show(message, connect_screen(url))
                        return
                except (ValueError, KeyError):
                    pass
                await show(message, await screens.render('home', user))
                return
            try:
                screen=await campaigns.claim(token,user)
            except CampaignRetry as error:
                screen=rich(escape(str(error)),[[('Повторить','url:'+campaigns.link(token))],
                                               [('Мои прокси','p:home')]])
            except ValueError as error:
                screen=rich(escape(str(error)),[[('Мои прокси','p:home')]])
            await show(message,screen);return
        elif message.text.startswith('/start') and message.text.partition(' ')[2].startswith('px_'):
            from app.services.public_proxy import connect_screen, connection_url
            url = await connection_url(screens.repo)
            if url:
                await show(message, connect_screen(url))
                return
            from app.services.proxy_invites import ProxyInvites
            try:screen=await ProxyInvites(screens.repo,screens.settings).claim(message.text.partition(' ')[2][3:],user)
            except ValueError as error:
                await message.answer(str(error),parse_mode=None,reply_markup=keyboard([[('Мои подключения','p:home')]]));return
            screen.structured = True
            screen.button_options.update({target: {'style': 'primary'} for row in screen.rows for _, target in row if target.startswith('url:')})
            await show(message,await screens.decorate("p:open",user,screen));return
        elif message.text.startswith('/start') and ' gift_' in message.text:
            from app.services.gifts import GiftService
            code = message.text.split(maxsplit=1)[1].removeprefix('gift_')
            delivery_key=f'gift_start:{message.chat.id}:{message.message_id}'
            if await screens.repo.setting(delivery_key)=='done':
                return
            try:
                screen = await GiftService(screens.repo, screens.settings).claim(code, user)
            except ValueError as error:
                await message.answer(str(error), parse_mode=None,reply_markup=keyboard([[('← Назад','home')]]))
                return
            if await screens.repo.setting(delivery_key)=='done':
                return
            screen.clear_keyboard=await screens.repo.setting(f'gift_keyboard:{user.id}')=='1'
            if screen.clear_keyboard:
                await screens.repo.set_setting(f'gift_keyboard:{user.id}','0')
            await show(message, await screens.decorate("receipt",user,screen))
            await screens.repo.set_setting(delivery_key,'done')
            return
        screen=await screens.render(page,user)
        await show(message,screen)

    @router.pre_checkout_query()
    async def stars_precheckout(query: PreCheckoutQuery, screens, user):
        from app.services.stars import StarService
        try:
            await StarService(screens.repo, screens.settings).precheckout(query, user)
        except ValueError as error:
            await query.answer(ok=False, error_message=str(error)[:200])
            return
        await query.answer(ok=True)

    @router.message(F.successful_payment)
    async def stars_success(message: Message, state: FSMContext, screens, user):
        await state.clear()
        payment = message.successful_payment
        if payment.currency != 'XTR':
            await message.answer('Платёж получен, но его валюта не поддерживается. Обратитесь в поддержку.')
            return
        from app.services.stars import StarService
        try:
            screen = await StarService(screens.repo, screens.settings).complete(payment, user)
        except ValueError as error:
            await message.answer(str(error), parse_mode=None)
            return
        await show(message, await screens.decorate('receipt',user,screen))

    @router.callback_query(F.data)
    async def callback(query: CallbackQuery, state: FSMContext, screens, user,callback_ack_started=False):
        if query.data.startswith('f21:'):
            from app.services.freeman_ai.v21.telegram import bot_callback
            await bot_callback(query,screens)
            return
        if not callback_ack_started:start_callback_ack(query)
        from app.services.paid_navigation import route, split as split_paid
        action, paid_sid = split_paid(query.data)
        if paid_sid is None and action.startswith('rename:'):
            markup = getattr(query.message, 'reply_markup', None)
            if markup and any(
                (button.callback_data or '').startswith('paid_help:')
                for row in markup.inline_keyboard for button in row
            ):
                paid_sid = int(action.split(':')[1])
        render_action = route(action, paid_sid) if paid_sid else action
        if action.startswith('campaign_deal:'):
            if query.message and query.message.chat.type=='private':
                from app.bot.delivery import fresh_context
                screen = await screens.render(action, user)
                await show(fresh_context(query), screen)
            return
        await state.clear()
        if action.startswith('checkout_check:') and query.message.chat.type=='private':
            from app.services.bot_checkout import BotCheckout
            pid = int(action.split(':')[1])
            checkout = BotCheckout(screens.repo, screens.settings)
            markup = query.message.reply_markup

            async def label(value, *, settled=False, bank_started=False):
                if not markup:
                    return
                rows = [[button.model_copy(update={'text': value}) if button.callback_data == query.data else button
                         for button in row
                         if not ((settled and button.url and '/pay/bot/' in button.url)
                                 or ((settled or bank_started) and button.callback_data == f'checkout_balance:{pid}'))]
                        for row in markup.inline_keyboard]
                try:
                    await query.message.edit_reply_markup(reply_markup=markup.model_copy(
                        update={'inline_keyboard': [row for row in rows if row]}))
                except TelegramBadRequest:
                    pass

            await label('⏳ Проверяем оплату…')
            try:
                payment = await checkout.owned(pid, user)
                if payment.provider_id and payment.status not in ('order_paid', 'order_canceled'):
                    data = await checkout.service.provider.get(payment.provider_id)
                    async with screens.repo.session.begin_nested():
                        await checkout.service.credit(payment, data)
                if payment.status == 'order_paid':
                    await label('✅ Оплачено', settled=True)
                elif payment.status == 'order_canceled':
                    await label('Счёт отменён', settled=True)
                else:
                    started = bool(payment.provider_id or await screens.repo.setting(f'bot_checkout_started:{pid}'))
                    await label('⏳ Оплата ещё не поступила' if started else 'Оплата ещё не начата', bank_started=started)
            except Exception as error:
                import logging
                logging.getLogger(__name__).warning('Checkout status check failed: %s', type(error).__name__)
                await label('Не удалось проверить · повторить')
            return
        if action=='widget_catalog':
            from app.bot.delivery import fresh_context
            try:
                if query.message.chat.type!='private' or query.message.chat.id!=user.telegram_id or user.status=='blocked':
                    return
                screen = await screens.render('catalog_groups:0:0', user)
                await show(fresh_context(query), screen)
                await query.message.delete()
            except TelegramBadRequest:
                return
            return
        if action=='widget_back':
            from app.services.bot_widget_queries import back,back_home
            try:await back(screens,user,query)
            except ValueError:
                await back_home(screens,user,query)
            except PermissionError:
                return
            return
        from app.services.admin_key_navigation import split, route
        base, origin = split(action)
        issue=base.removeprefix('admin:issue:')
        if issue!=base and issue.isascii() and issue.isdecimal() and query.message.chat.type=='private' and screens.settings.is_owner(user.telegram_id):
            await show(query, await screens.render(route(base+':list',origin),user))
            return
        if action in ('admin:widgetdemo','admin:widgetdemo:send') and screens.settings.is_owner(user.telegram_id):
            await show(query, await screens.render('buy',user))
            return
        if action.startswith(('admin:t:','t:')):
            from app.services.tickets import TicketService
            if action.startswith('admin:t:') and not screens.settings.is_admin(user.telegram_id):return
            mode='staff' if action.startswith('admin:t:') else 'new' if action=='t:input:new' else 'list'
            await screens.repo.set_setting(f'support_dialog:{user.id}','0')
            await show(query,TicketService(screens.repo,screens.settings).mini_screen(user,mode))
            return
        if query.message.chat.type=='private' and not action.startswith('t:') and await screens.repo.setting(f'support_dialog:{user.id}')=='1':
            await screens.repo.set_setting(f'support_dialog:{user.id}','0')
        if action.startswith('admin:') and not screens.settings.can_admin_action(user.telegram_id, __import__('app.services.list_pages',fromlist=['split_action']).split_action(action)[0]):
            await query.message.answer('Этот раздел недоступен.')
            return
        if action.startswith('stars:'):
            if query.message.chat.type != 'private':
                await query.message.answer('Оплата доступна в личном чате с ботом.')
                return
            if screens.settings.role(user.telegram_id)=='partner':
                await show(query,await screens.render('partner:home',user))
                return
            parts = action.split(':')
            try:
                months = parts[1]
                sid = int(parts[2]) if len(parts) > 2 else 0
                from app.services.stars import StarService
                antig = parts[3:] == ['antig']
                if len(parts) > 3 and not antig:
                    raise ValueError('Тариф недоступен.')
                invoice = await StarService(screens.repo, screens.settings).create(user, months, sid, antig=antig)
                await query.message.bot.send_invoice(chat_id=query.message.chat.id, **invoice)
                try:
                    await query.message.delete()
                except TelegramBadRequest:
                    pass
            except (ValueError, IndexError) as error:
                await query.message.answer(str(error) if isinstance(error, ValueError) else 'Тариф недоступен.', parse_mode=None)
            return
        if query.message.chat.type != 'private' and action.startswith('admin:t:input:reply:'):
            from app.services.forum import current_topic_ticket
            item = await current_topic_ticket(screens.repo, query.message.chat.id, query.message.message_thread_id)
            if not item or item.id != int(action.split(':')[-1]):
                await query.message.answer('Откройте тему нужного клиента. Ответы отправляются только из его темы.')
                return
            # Bot administrator receives ordinary messages in topics, no shared FSM across clients.
            return
        if query.message.chat.type != 'private' and action.startswith(('admin:t:topicclose:', 'admin:t:closeconfirm:', 'admin:t:close:')):
            from app.services.forum import current_topic_ticket
            from app.services.tickets import TicketService
            item = await current_topic_ticket(screens.repo, query.message.chat.id, query.message.message_thread_id)
            if not item:
                await query.message.answer('В этой теме нет открытого обращения.')
                return
            expected = item.user_id if action.startswith('admin:t:topicclose:') else item.id
            if int(action.split(':')[-1]) != expected:
                await query.message.answer('Это действие относится к другому клиенту. Откройте его тему.')
                return
            if action.startswith('admin:t:close:'):
                if item.id != int(action.split(':')[-1]):
                    return
                await TicketService(screens.repo, screens.settings).render(action, user)
                await query.message.edit_text('✅ Обращение закрыто. Тема будет закрыта после доставки оставшихся сообщений.')
            else:
                await confirm_topic_close(query.message, item)
            return
        if query.message.chat.type != 'private' and action == 'admin:t:closecancel':
            await query.message.edit_text('Закрытие отменено.')
            return
        try:
            screen = await screens.render(render_action, user)
        except (ValueError, IndexError, KeyError) as error:
            if action.startswith('admin:proxycampaign:'):
                from html import escape
                from app.services.admin_client_grant import rich
                rows=[[('← Рекламные ссылки','admin:proxycampaign')]]
                if action.startswith('admin:proxycampaign:make:'):
                    rows.insert(0,[('Повторить',action)])
                await show(query,rich(escape(str(error)) if isinstance(error,ValueError) else 'Ссылка недоступна.',rows))
                return
            if action.startswith('admin:partnerdebt:'):
                from app.services.admin_client_grant import rich
                from html import escape
                await show(query,rich(escape(str(error)) if isinstance(error,ValueError) else 'Операция недоступна.',
                                      [[('← Расчёты с партнёрами','admin:partnerdebt')]]))
                return
            if action.startswith('cg:'):
                from app.services.bot_gift_links import styled
                from html import escape
                await show(query,styled('🎁 <b>Подарок</b>\n\n'+escape(str(error) if isinstance(error,ValueError) else 'Откройте подарок заново.'),[[('🔄 Повторить',action)],[('🎁 Мои подарки','cg:list:0')]]))
                return
            if action.startswith('up:'):
                from app.services.bot_upgrade import failure
                await show(query,failure(action,error))
                return
            if action.startswith(('admin:proxygrant:', 'admin:grantconfig:', 'admin:giftlink:')) and isinstance(error, ValueError):
                from app.services.admin_client_grant import rich
                from html import escape
                screen = rich('⚠️ <b>Не удалось выполнить действие</b>\n\n'+escape(str(error)),
                    [[('🔄 Повторить', action)], [('← Назад', 'p:home' if ':proxygrant:' in action else 'buy')]])
                await show(query, screen)
                return
            if action.startswith('t:') and isinstance(error,ValueError):
                from app.services.screens import Screen
                await show(query,Screen(str(error),[[('Мои обращения','t:list:0')],[('Главное меню','home')]]))
                return
            screen = screens.missing()
        if screen.admin_input_action:
            await state.set_state(AdminInput.waiting)
            await state.update_data(admin_action=screen.admin_input_action)
        if action.startswith('promo:'):
            await state.set_state(PromoInput.waiting)
            await state.update_data(return_to=action.partition(':')[2])
        if action=='topup_input':
            await state.set_state(TopupInput.waiting)
        if action.startswith('admin:input:') and screens.settings.can_admin_action(user.telegram_id, __import__('app.services.list_pages',fromlist=['split_action']).split_action(action)[0]):
            await state.set_state(AdminInput.waiting)
            await state.update_data(admin_action=action)
        if action.startswith(('admin:x:input:', 'x:input:', 'admin:p:input:', 'admin:v:input:', 'admin:r:input:')):
            await state.set_state(ExtendedInput.waiting)
            await state.update_data(extended_action=action)
        if action.startswith('g:choose:'):
            p=action.split(':')
            from app.services.gifts import GiftService
            try:
                await GiftService(screens.repo,screens.settings).render(f'g:input:{p[2]}:{p[3]}',user)
            except ValueError:
                await show(query,screens.missing())
                return
            if p[4]!='pick':
                await state.set_state(GiftInput.waiting)
                await state.update_data(gift_action=f'g:input:{p[2]}:{p[3]}',gift_picker=False)
        if action.startswith('rename:'):
            try:
                subscription_id = int(action.split(':')[1])
            except (ValueError, IndexError):
                subscription_id = 0
            if await screens.repo.subscription(user.id, subscription_id):
                await state.set_state(SubscriptionNameInput.waiting)
                await state.update_data(subscription_id=subscription_id, paid_rename=paid_sid == subscription_id)
        if action.startswith(('admin:t:input:', 't:input:')):
            from app.services.tickets import TicketService
            try:
                await TicketService(screens.repo, screens.settings).render(action, user)
            except (ValueError, IndexError, KeyError):
                await show(query, screens.missing())
                return
            await state.set_state(TicketInput.waiting)
            await state.update_data(ticket_action=action)
            if query.message.chat.type != 'private':
                await query.message.answer(screen.text, parse_mode='HTML', reply_markup=ForceReply(input_field_placeholder='Ответ клиенту'))
                return
        await show(query, screen)

    async def explicit_search(message: Message, state: FSMContext, screens, user, command=None):
        from app.services.bot_admin_search import BotAdminSearch, PROMPT
        if message.chat.type != 'private' or not screens.settings.is_owner(user.telegram_id):
            from aiogram.dispatcher.event.bases import SkipHandler
            raise SkipHandler
        await state.clear()
        value = (command.args or '') if command else (message.text or '')
        if not value.strip():
            await message.answer(PROMPT, parse_mode='HTML')
            return
        try:
            screen = await BotAdminSearch(screens.repo, screens.settings).find(value, user)
        except ValueError as error:
            await message.answer(str(error), parse_mode=None)
            return
        await show(message, screen)

    from app.services.bot_admin_search import telegram_identifier
    router.message.register(explicit_search, Command('find'))
    router.message.register(explicit_search, F.text.func(lambda value: telegram_identifier(value) is not None))

    @router.message(TopupInput.waiting)
    async def topup_input(message: Message,state: FSMContext,screens,user):
        from decimal import Decimal,InvalidOperation
        from app.services.topups import TopupService
        try:
            amount=Decimal((message.text or '').strip().replace(',','.'))*100
            if not amount.is_finite() or amount!=amount.to_integral_value() or not 100<=amount<=10000000:
                raise ValueError('Введите сумму от 1 до 100 000 ₽, не более двух знаков после запятой.')
            screen=await TopupService(screens.repo,screens.settings).prepare(user,int(amount))
        except (InvalidOperation,ValueError) as error:
            await message.answer(str(error) if isinstance(error,ValueError) else 'Неверная сумма. Например: 500.50',parse_mode=None)
            return
        await state.clear()
        await show(message,await screens.decorate('topup_pay',user,screen))

    @router.message(TicketInput.waiting)
    async def ticket_input(message: Message, state: FSMContext, screens, user, group_context=False):
        from app.services.tickets import TicketService
        await state.clear()
        await screens.repo.set_setting(f'support_dialog:{user.id}','0')
        await show(message,TicketService(screens.repo,screens.settings).mini_screen(user,'staff' if screens.settings.can_admin_action(user.telegram_id,'admin:t:home') else 'new'))
        return

    @router.message(ExtendedInput.waiting)
    async def extended_input(message: Message, state: FSMContext, screens, user):
        from app.services.control import ControlService
        data = await state.get_data()
        action = data.get('extended_action', '')
        if action.startswith('admin:') and not screens.settings.can_admin_action(user.telegram_id, __import__('app.services.list_pages',fromlist=['split_action']).split_action(action)[0]):
            await state.clear()
            await message.answer('Этот раздел недоступен.')
            return
        if action == 'admin:r:input:server' or (action.startswith('admin:r:input:panel:') and action.endswith(':api_token')):
            try:
                await asyncio.wait_for(message.delete(), timeout=3)
            except (TelegramAPIError, TimeoutError):
                pass
        try:
            if action.startswith('admin:p:'):
                from app.services.proxies import ProxyService
                screen = await ProxyService(screens.repo, screens.settings).input(action, user, message.text or '')
            elif action.startswith('admin:v:'):
                from app.services.vpn import VpnService
                screen = await VpnService(screens.repo, screens.settings).input(action, user, message.text or '')
            elif action.startswith('admin:r:'):
                from app.services.remnawave import RemnawaveService
                screen = await RemnawaveService(screens.repo, screens.settings).input(action, user, message.text or '')
            else:
                media=('photo',message.photo[-1].file_id) if message.photo else ('animation',message.animation.file_id) if message.animation else ('video',message.video.file_id) if message.video else None
                campaign_photo=data.get('campaign_photo')
                content=None
                if action.startswith(('admin:x:input:campaign:','admin:x:input:campaignedit:')):
                    if message.photo and not message.caption and not getattr(message,'rich_message',None):
                        await state.update_data(campaign_photo=message.photo[-1].file_id)
                        await message.answer('📷 Фото добавлено. Теперь пришлите текст с форматированием и отступами. До подтверждения рассылка не отправляется.',parse_mode=None)
                        return
                    from app.services.campaign_content import capture
                    content=capture(message,campaign_photo)
                input_text=message.text or message.caption or ''
                if action.startswith('admin:x:input:campaignbuttonicon:'):
                    input_text=(getattr(message.sticker,'custom_emoji_id',None) if message.sticker else None) or next((entity.custom_emoji_id for entity in ((message.entities or [])+(message.caption_entities or [])) if getattr(entity.type,'value',entity.type)=='custom_emoji' and entity.custom_emoji_id),input_text)
                screen = await ControlService(screens.repo, screens.settings).input(action, user,
                    input_text, message.photo[-1].file_id if message.photo else campaign_photo, media,content=content)
        except (ValueError, KeyError, IndexError) as error:
            await message.answer(str(error) if isinstance(error, ValueError) else 'Действие недоступно.', parse_mode=None)
            return
        if screen.keep_input:
            await state.set_state(ExtendedInput.waiting)
            await state.update_data(extended_action=action)
        else:
            await state.clear()
        if action.startswith('x:input:withdraw') or action.startswith(('admin:p:input:','admin:v:input:')):
            try:
                await message.delete()
            except TelegramBadRequest:
                pass
        await show(message, await screens.decorate(action,user,screen))

    @router.message(GiftInput.waiting)
    async def gift_input(message: Message, state: FSMContext, screens, user):
        from app.services.gifts import GiftService
        data = await state.get_data()
        if message.text=='✍️ Ввести username или ID':
            await message.answer('Отправьте @username или числовой Telegram ID одним сообщением.',reply_markup=ReplyKeyboardRemove())
            await screens.repo.set_setting(f'gift_keyboard:{user.id}','0')
            return
        if message.users_shared and (not data.get('gift_picker') or message.users_shared.request_id!=1731 or len(message.users_shared.users)!=1):
            await message.answer('Выберите одного друга с помощью кнопки.')
            return
        if message.contact and not message.contact.user_id:
            await message.answer('У этого контакта нет Telegram ID. Укажите числовой ID или зарегистрированный @username.')
            return
        try:
            screen = await GiftService(screens.repo, screens.settings).input(data.get('gift_action',''),user,
                message.text or '',message.users_shared.users[0].user_id if message.users_shared else message.contact.user_id if message.contact else None)
        except (ValueError,IndexError,KeyError) as error:
            await message.answer(str(error) if isinstance(error,ValueError) else 'Действие недоступно.',parse_mode=None,reply_markup=keyboard([[('← Назад',data.get('gift_action','g:home'))]]))
            return
        await state.clear()
        screen.clear_keyboard=await screens.repo.setting(f'gift_keyboard:{user.id}')=='1'
        await screens.repo.set_setting(f'gift_keyboard:{user.id}','0')
        await show(message,await screens.decorate('g:home',user,screen))

    @router.message(SubscriptionNameInput.waiting)
    async def subscription_name(message: Message, state: FSMContext, screens, user):
        data = await state.get_data()
        subscription_id = data.get('subscription_id', 0)
        target = f'paid_receipt:{subscription_id}' if data.get('paid_rename') else f'sub:{subscription_id}'
        if not await screens.repo.subscription(user.id, subscription_id):
            await state.clear()
            await show(message, screens.missing())
            return
        try:
            await screens.subscriptions.rename(user.id, subscription_id, message.text or '')
        except ValueError as error:
            await message.answer(str(error), reply_markup=keyboard([[('← Назад', target)]]), parse_mode=None)
            return
        await state.clear()
        screen = await screens.render(target, user)
        screen.text = '✅ Название сохранено.\n\n' + screen.text
        await show(message, screen)

    @router.message(AdminInput.waiting)
    async def admin_input(message: Message, state: FSMContext, screens, user):
        if not screens.settings.is_admin(user.telegram_id):
            await state.clear()
            await message.answer('Этот раздел недоступен.')
            return
        data = await state.get_data()
        from app.services.admin import AdminService
        try:
            action=data.get('admin_action','')
            value=message.text or ''
            if action.startswith('admin:input:button_icon:'):
                value=(getattr(message.sticker,'custom_emoji_id',None) if message.sticker else None) or next((entity.custom_emoji_id for entity in ((message.entities or [])+(message.caption_entities or [])) if getattr(entity.type,'value',entity.type)=='custom_emoji' and entity.custom_emoji_id),value)
            screen = await AdminService(screens.repo, screens.settings).apply_input(action, value, user)
        except (ValueError, KeyError, IndexError) as error:
            back = action if action in ('admin:input:proxycampaign','admin:input:public_proxy') or action.startswith(('admin:input:grantconfig:', 'admin:input:proxygrant:', 'admin:input:partnerdebt:', 'admin:input:discount:')) else 'admin:home'
            await message.answer(str(error) if isinstance(error, ValueError) else 'Действие недоступно.', reply_markup=keyboard([[('← Назад', back)]]), parse_mode=None)
            return
        await state.clear()
        if action.startswith('admin:input:payment_'):
            try:
                await message.delete()
            except TelegramBadRequest:
                pass
        if screen.admin_input_action:
            await state.set_state(AdminInput.waiting)
            await state.update_data(admin_action=screen.admin_input_action)
        await show(message, screen)

    @router.message(PromoInput.waiting)
    async def promo(message: Message, state: FSMContext, screens, user):
        if not message.text or len(message.text) > 64:
            await message.answer('Введите промокод текстом (до 64 символов).')
            return
        data = await state.get_data()
        result = await screens.promos.apply(user.id, message.text)
        if not result:
            await message.answer('❌ Промокод не найден или уже использован.', reply_markup=keyboard([[('← Назад', data.get('return_to', 'balance'))]]))
            return
        await state.clear()
        target = data.get('return_to', 'balance')
        screen = await screens.render(target, user)
        screen.text = f'✅ Промокод применён.\nСкидка {result.value}% на подписку.\n\n' + screen.text
        await show(message, screen)

    @router.message()
    async def fallback(message: Message, state: FSMContext, screens, user, group_context=False):
        await state.clear()
        if group_context:
            await topic_reply(message, screens, user)
            return
        if not screens.settings.is_admin(user.telegram_id):
            from app.services.tickets import TicketService
            replied = await TicketService(screens.repo, screens.settings).client_reply_ticket(user,message.chat.id,
                message.reply_to_message.message_id if message.reply_to_message else None)
            if replied or await screens.repo.setting(f'support_dialog:{user.id}','0')=='1':
                await state.set_state(TicketInput.waiting)
                await state.update_data(ticket_action='t:input:new')
                await ticket_input(message,state,screens,user)
                return
        if screens.settings.can_admin_action(user.telegram_id, 'admin:t:home') and message.reply_to_message:
            from app.services.tickets import TicketService
            service = TicketService(screens.repo, screens.settings)
            ticket_id = await service.replied_ticket(message.chat.id, message.reply_to_message.message_id, user)
            if ticket_id:
                try:
                    screen = await service.input(f'admin:t:input:reply:{ticket_id}', user,
                        message.text or message.caption or '', f'{message.chat.id}:{message.message_id}',
                        message.photo[-1].file_id if message.photo else None, message.document.file_id if message.document else message.video.file_id if message.video else None)
                except ValueError as error:
                    await message.answer(str(error), parse_mode=None)
                    return
                await message.answer('✅ Ответ принят в очередь.', parse_mode=None)
                return
        if message.chat.type=='private' and message.text and (screens.settings.is_owner(user.telegram_id) or screens.settings.role(user.telegram_id)=='partner'):
            from app.services.bot_admin_search import BotAdminSearch,automatic
            if automatic(message.text) or (screens.settings.is_owner(user.telegram_id) and not message.text.startswith('/') and await screens.repo.setting(f'ai_dialog:{user.id}','0')!='1'):
                try:
                    if screens.settings.role(user.telegram_id)=='partner':
                        from app.services.partner_bot import PartnerService
                        screen=await PartnerService(screens.repo,screens.settings).find(message.text,user)
                    else:screen=await BotAdminSearch(screens.repo,screens.settings).find(message.text,user)
                except ValueError as error:
                    target='partner:search' if screens.settings.role(user.telegram_id)=='partner' else 'admin:input:search'
                    screen=__import__('app.services.screens',fromlist=['Screen']).Screen(str(error),[[('🔎 Поиск',target)],[('← Главное меню','home')]],structured=True)
                await show(message,screen)
                return
        if message.chat.type=='private' and message.text and not message.text.startswith('/'):
            from app.services.freeman_ai.service import channel_enabled,conversation,append
            if await channel_enabled(screens.repo.session,screens.settings,'telegram') and await screens.repo.setting(f'ai_dialog:{user.id}','0')=='1':
                from app.services.freeman_ai.identity import channel_identity
                identity=await channel_identity(screens.repo.session,screens.settings,'telegram',str(message.from_user.id))
                conv=await conversation(screens.repo.session,screens.settings,identity=identity)
                await append(screens.repo.session,screens.settings,conv,message.text[:3500],f'{message.chat.id}:{message.message_id}')
                return
        await show(message, await screens.render('home', user))

    async def topic_reply(message, screens, user):
        if message.text and message.text.startswith('/'):
            return
        if not message.text and not message.caption and not message.photo and not message.document and not message.video:
            return
        from app.services.forum import current_topic_ticket
        from app.services.tickets import TicketService
        item = await current_topic_ticket(screens.repo, message.chat.id, message.message_thread_id)
        if not item:
            await message.answer('Откройте тему клиента с действующим обращением. В «Общее» ответы клиентам не отправляются.')
            return
        service = TicketService(screens.repo, screens.settings)
        if message.reply_to_message:
            replied = await service.replied_ticket(message.chat.id, message.reply_to_message.message_id, user)
            if replied and replied != item.id:
                await message.answer('Сообщение относится к другому обращению. Откройте тему нужного клиента.')
                return
        try:
            await service.input(f'admin:t:input:reply:{item.id}', user,
                message.text or message.caption or '', f'{message.chat.id}:{message.message_id}',
                message.photo[-1].file_id if message.photo else None, message.document.file_id if message.document else message.video.file_id if message.video else None)
        except ValueError as error:
            await message.answer(str(error), parse_mode=None)
            return
        try:
            await message.bot.set_message_reaction(message.chat.id, message.message_id, reaction=[ReactionTypeEmoji(emoji='👍')])
        except TelegramAPIError:
            pass

    async def confirm_topic_close(message, item):
        await message.answer(f'Закрыть обращение #{item.id} и тему клиента?',
            reply_markup=keyboard([[('✅ Закрыть', f'admin:t:close:{item.id}')], [('Отмена', 'admin:t:closecancel')]]))

    return router

