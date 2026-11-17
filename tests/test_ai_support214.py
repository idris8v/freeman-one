import unittest
from sqlalchemy import select,func
from test_ai_owner import OwnerTests
from app.database.models import User,Ticket,TicketMessage,WebSupportAttachment,ChatNotice,ForumTask,AIConversation,AppSetting
from app.database.repositories import SQLRepository
from app.services.freeman_ai import owner
from app.services.tickets import TicketService

class Support214(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=OwnerTests.asyncSetUp
 asyncTearDown=OwnerTests.asyncTearDown
 async def test_open_tickets_tool_and_customer_denial(self):
  async with self.db.sessions.begin() as s:
   for status in ['closed','waiting_admin','waiting_client','needs_info','in_progress']:s.add(Ticket(user_id=self.other,status=status))
   await s.flush();c=await owner.conversation(s,self.settings,await s.get(User,self.uid));r=await owner.execute(s,self.settings,c,'list_tickets',{'status':'open'});self.assertEqual(r['total'],4);self.assertNotIn('closed',[t['status'] for t in r['items']])
   self.settings.is_owner=lambda tid:False
   with self.assertRaises(PermissionError):await owner.execute(s,self.settings,c,'list_tickets',{'status':'open'})
 async def test_delete_shared_ticket_and_dependencies(self):
  async with self.db.sessions.begin() as s:
   self.settings.can_admin_action=lambda tid,*args:tid==123
   u=await s.get(User,self.uid);other=await s.get(User,self.other);repo=SQLRepository(s)
   t=Ticket(user_id=self.other);s.add(t);await s.flush();tid=t.id
   m=TicketMessage(ticket_id=tid,role='client',text='Вопрос',source='source');s.add(m);await s.flush()
   s.add(WebSupportAttachment(message_id=m.id,kind='photo',mime_type='image/png',original_name='photo.png',storage_name='test.png',size=10))
   s.add(ChatNotice(ticket_id=tid,dedup='notice',target_id=456,kind='support',text='test'))
   s.add(ForumTask(ticket_id=tid));c=AIConversation(user_id=self.other,channel='website',ticket_id=tid);s.add(c)
   await repo.set_setting('support_ticket:'+str(self.other),str(tid));await repo.set_setting('support_dialog:'+str(self.other),'1');await s.flush()
   svc=TicketService(repo,self.settings)
   with self.assertRaises(PermissionError):await svc.delete(tid,other)
   await svc.delete(tid,u);await s.flush()
   self.assertIsNone(await s.get(Ticket,tid));self.assertEqual(c.mode,'closed');self.assertIsNone(c.ticket_id)
   for model in (TicketMessage,WebSupportAttachment,ChatNotice,ForumTask):self.assertEqual(await s.scalar(select(func.count()).select_from(model)),0)
   self.assertEqual(await repo.setting('support_dialog:'+str(self.other)),'0')
