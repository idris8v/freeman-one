"""Offline channel routing regression tests; no real Telegram messages."""
import unittest
from unittest.mock import AsyncMock,patch
from types import SimpleNamespace
from datetime import timedelta
from sqlalchemy import select,func
from test_freeman_ai import Tests as Fixture
from app.config.settings import Settings
from app.database.models import User,Ticket,TicketMessage,ChatNotice,now
from app.database.repositories import SQLRepository
from app.services.tickets import TicketService
from app.services.notices import NoticeWorker

class SupportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=Fixture.asyncSetUp
    asyncTearDown=Fixture.asyncTearDown

    def service(self,s):
        return TicketService(SQLRepository(s),Settings(admin_ids='999',encryption_key=self.settings.encryption_key))

    async def test_site_to_telegram_same_ticket_and_delivery(self):
        async with self.db.sessions.begin() as s:
            svc=self.service(s);user=await s.get(User,self.uid)
            await svc.input('t:input:new',user,'С сайта','web:one',force_new=True)
            ticket=await svc.current_dialog(user)
            await svc.input('t:input:new',user,'Из Telegram','123:2',photo='test-file')
            self.assertEqual(await s.scalar(select(func.count()).select_from(Ticket)),1)
            admin=User(telegram_id=999,first_name='Admin');s.add(admin);await s.flush()
            await svc.input(f'admin:t:input:reply:{ticket.id}',admin,'Ответ с сайта','web:admin')
            notice=await s.scalar(select(ChatNotice).where(ChatNotice.kind=='client'))
            self.assertEqual(notice.target_id,123);self.assertEqual(notice.ticket_id,ticket.id)
            self.assertEqual(ticket.status,'waiting_client')
            messages=list(await s.scalars(select(TicketMessage).where(TicketMessage.ticket_id==ticket.id)))
            self.assertEqual(len(messages),3);self.assertEqual(messages[1].photo,'test-file')

    async def test_reply_to_exact_ticket_and_ownership(self):
        async with self.db.sessions.begin() as s:
            svc=self.service(s);user=await s.get(User,self.uid)
            a=Ticket(user_id=user.id);b=Ticket(user_id=user.id);s.add_all([a,b]);await s.flush()
            await svc.select_dialog(user,b)
            s.add(ChatNotice(dedup='reply',target_id=123,kind='client',ticket_id=a.id,text='answer',message_id=40,media_message_id=39))
            await s.flush()
            for mid in (39,40):self.assertEqual((await svc.client_reply_ticket(user,123,mid)).id,a.id)
            self.assertIsNone(await svc.client_reply_ticket(await s.get(User,self.other),123,40))
            await svc.input(f't:input:reply:{a.id}',user,'Exact reply','123:41')
            saved=await s.scalar(select(TicketMessage).where(TicketMessage.source=='123:41'))
            self.assertEqual(saved.ticket_id,a.id)
            self.assertEqual(await svc.repo.setting(f'support_dialog:{user.id}'),'0')

    async def test_ambiguous_unselected_tickets_never_choose_latest(self):
        async with self.db.sessions.begin() as s:
            svc=self.service(s);user=await s.get(User,self.uid)
            s.add_all([Ticket(user_id=user.id),Ticket(user_id=user.id)]);await s.flush()
            with self.assertRaisesRegex(ValueError,'несколько'):await svc.current_dialog(user)

    async def test_close_clears_mode_queues_home_once_and_does_not_reopen(self):
        async with self.db.sessions.begin() as s:
            svc=self.service(s);user=await s.get(User,self.uid)
            await svc.input('t:input:new',user,'Question','123:1');ticket=await svc.current_dialog(user)
            await svc.set_status(ticket.id,user,'closed');await svc.set_status(ticket.id,user,'closed')
            self.assertEqual(await svc.repo.setting(f'support_dialog:{user.id}'),'0')
            self.assertEqual(await s.scalar(select(func.count()).select_from(ChatNotice).where(ChatNotice.kind=='support_home')),1)
            with self.assertRaises(ValueError):await svc.input(f't:input:reply:{ticket.id}',user,'late','123:9')
            self.assertEqual(ticket.status,'closed')

    async def test_closing_other_ticket_keeps_selected_dialog(self):
        async with self.db.sessions.begin() as s:
            svc=self.service(s);user=await s.get(User,self.uid)
            a=Ticket(user_id=user.id);b=Ticket(user_id=user.id);s.add_all([a,b]);await s.flush()
            await svc.select_dialog(user,b);await svc.set_status(a.id,user,'closed')
            self.assertEqual((await svc.current_dialog(user)).id,b.id)
            self.assertEqual(await svc.repo.setting(f'support_dialog:{user.id}'),'1')
            self.assertEqual(await s.scalar(select(func.count()).select_from(ChatNotice).where(ChatNotice.kind=='support_home')),0)

    async def test_idle_close_queues_home_and_preserves_waiting_support(self):
        async with self.db.sessions.begin() as s:
            user=await s.get(User,self.uid);a=Ticket(user_id=user.id,status='waiting_client',updated_at=now()-timedelta(hours=25));b=Ticket(user_id=self.other,status='waiting_admin',updated_at=now()-timedelta(hours=25));s.add_all([a,b]);await s.flush();aid=a.id;bid=b.id
            await self.service(s).select_dialog(user,a)
        worker=NoticeWorker(self.db,SimpleNamespace());self.assertEqual(await worker.expire_idle(),1)
        async with self.db.sessions() as s:
            self.assertEqual((await s.get(Ticket,aid)).status,'closed');self.assertEqual((await s.get(Ticket,bid)).status,'waiting_admin')
            self.assertEqual(await SQLRepository(s).setting(f'support_dialog:{self.uid}'),'0')
            self.assertEqual(await s.scalar(select(func.count()).select_from(ChatNotice).where(ChatNotice.kind=='support_home')),1)

    async def test_home_worker_suppresses_stale_menu_after_new_dialog(self):
        async with self.db.sessions.begin() as s:
            svc=self.service(s);user=await s.get(User,self.uid);a=Ticket(user_id=user.id);s.add(a);await s.flush();await svc.select_dialog(user,a)
            await svc.set_status(a.id,user,'closed')
            b=Ticket(user_id=user.id);s.add(b);await s.flush();await svc.select_dialog(user,b)
        worker=NoticeWorker(self.db,SimpleNamespace());worker.send_home=AsyncMock()
        # Client-close notification to configured admin precedes home; remove only test fixture notice.
        async with self.db.sessions.begin() as s:
            from sqlalchemy import delete
            await s.execute(delete(ChatNotice).where(ChatNotice.kind!='support_home'))
        self.assertTrue(await worker.once());worker.send_home.assert_not_awaited()

    async def test_retired_home_notice_is_canceled_in_miniapp_mode(self):
        async with self.db.sessions.begin() as s:
            a=Ticket(user_id=self.uid,status='closed');s.add(a);await s.flush()
            s.add(ChatNotice(dedup='home-test',kind='support_home',ticket_id=a.id,target_id=123,text=''))
        worker=NoticeWorker(self.db,SimpleNamespace());worker.send_home=AsyncMock()
        self.assertTrue(await worker.once());worker.send_home.assert_not_awaited()
        async with self.db.sessions() as s:
            self.assertEqual((await s.scalar(select(ChatNotice))).status,'cancelled')

    async def test_delivered_reply_opens_exact_miniapp_chat_without_bot_fsm(self):
        async with self.db.sessions.begin() as s:
            a=Ticket(user_id=self.uid);s.add(a);await s.flush();aid=a.id
            await SQLRepository(s).set_setting(f'support_dialog:{self.uid}','0')
            s.add(ChatNotice(dedup='answer',kind='client',ticket_id=a.id,target_id=123,text='Ответ'))
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=75)))
        settings=Settings(encryption_key=self.settings.encryption_key)
        worker=NoticeWorker(self.db,bot,settings);worker.forum.refresh_ticket=AsyncMock(return_value=None)
        self.assertTrue(await worker.once());bot.send_message.assert_awaited_once()
        self.assertIn('Откройте чат поддержки',bot.send_message.call_args.args[1])
        button=bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
        from urllib.parse import urlsplit,parse_qs
        query=parse_qs(urlsplit(button.web_app.url).query)
        self.assertEqual(query['ticket'],[str(aid)])
        async with self.db.sessions() as s:
            svc=self.service(s);user=await s.get(User,self.uid)
            self.assertEqual(await svc.repo.setting(f'support_dialog:{self.uid}'),'0')
            self.assertEqual((await svc.current_dialog(user)).id,aid)
            self.assertEqual((await svc.client_reply_ticket(user,123,75)).id,aid)

    async def test_real_telegram_handler_clears_stale_fsm_and_opens_miniapp(self):
        from app.bot.handlers.navigation import create_router
        handler=next(h.callback for h in create_router().message.handlers if h.callback.__name__=='ticket_input')
        async with self.db.sessions.begin() as s:
            svc=self.service(s);user=await s.get(User,self.uid)
            a=Ticket(user_id=user.id);b=Ticket(user_id=user.id);s.add_all([a,b]);await s.flush()
            await svc.select_dialog(user,b)
            s.add(ChatNotice(dedup='handler-reply',target_id=123,kind='client',ticket_id=a.id,text='Answer',message_id=50));await s.flush()
            state=SimpleNamespace(get_data=AsyncMock(return_value={'ticket_action':f't:input:reply:{b.id}'}),clear=AsyncMock(),set_state=AsyncMock(),update_data=AsyncMock())
            message=SimpleNamespace(chat=SimpleNamespace(id=123),message_id=51,reply_to_message=SimpleNamespace(message_id=50),text='Reply to A',caption=None,photo=None,document=None,video=None,bot=SimpleNamespace(set_message_reaction=AsyncMock()),answer=AsyncMock())
            screens=SimpleNamespace(repo=svc.repo,settings=svc.settings)
            with patch('app.bot.handlers.navigation.show',new=AsyncMock()) as show:
                await handler(message,state,screens,user)
                show.assert_awaited_once()
                self.assertTrue(any(target.startswith('webapp:') for row in show.call_args.args[1].rows for label,target in row))
            saved=await s.scalar(select(TicketMessage).where(TicketMessage.source=='123:51'))
            self.assertIsNone(saved)
            state.clear.assert_awaited_once()
            state.update_data.assert_not_awaited()

    async def test_delete_cancels_ai_work_and_preserves_customer_credentials(self):
        from app.database.models import AIConversation,AIMessage,AIJob
        async with self.db.sessions.begin() as s:
            user=await s.get(User,self.uid);user.password_hash='existing-user-password';user.auth_version=7
            a=Ticket(user_id=user.id);s.add(a);await s.flush();aid=a.id
            c=AIConversation(user_id=user.id,channel='website',ticket_id=aid);s.add(c);await s.flush();cid=c.id
            m=AIMessage(conversation_id=cid,role='client',text='question',source_hash='x'*64);s.add(m);await s.flush()
            job=AIJob(message_id=m.id,generation=0);s.add(job)
            s.add(TicketMessage(ticket_id=aid,role='client',text='question',source='delete-test'))
            admin=User(telegram_id=999);s.add(admin);await s.flush()
            with self.assertRaises(PermissionError):await self.service(s).delete(aid,user)
            await self.service(s).delete(aid,admin)
        async with self.db.sessions() as s:
            user=await s.get(User,self.uid)
            self.assertEqual(user.password_hash,'existing-user-password');self.assertEqual(user.auth_version,7)
            self.assertIsNone(await s.get(Ticket,aid))
            self.assertEqual((await s.get(AIConversation,cid)).mode,'closed')
            self.assertEqual((await s.scalar(select(AIJob))).status,'canceled')
            self.assertEqual(await s.scalar(select(func.count()).select_from(TicketMessage)),0)
