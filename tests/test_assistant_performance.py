"""Query-count and ownership regressions, with isolated PostgreSQL metadata."""
import json
import os
import unittest

import httpx
from fastapi import FastAPI
from sqlalchemy import event
from test_ai_owner import OwnerTests

from app.database.models import AIConversation, AIMessage, AIOwnerFile, AIOwnerImage, AppSetting, User
from app.services.freeman_ai import service
from app.web.freeman_ai import ai_router


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "isolated PostgreSQL")
class AssistantPerformance(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = OwnerTests.asyncSetUp
    asyncTearDown = OwnerTests.asyncTearDown

    async def test_polling_uses_bounded_queries_without_document_contents(self):
        async with self.db.sessions.begin() as s:
            c = AIConversation(user_id=self.uid, channel="owner", specialist="owner"); s.add(c); await s.flush(); cid = c.id
            for i in range(100):
                m = AIMessage(conversation_id=cid, role="assistant", source_hash=f"message-{i}", text=f"Message {i}", actions=json.dumps([{"type":"site_change","summary":"Done","operations":[]}]))
                s.add(m); await s.flush()
                s.add(AIOwnerFile(message_id=m.id, name=f"doc{i}.txt", size=1, mime="text/plain", encrypted_data="must-not-be-selected"))
                s.add(AIOwnerImage(message_id=m.id, encrypted_data="must-not-be-selected"))
                s.add(AppSetting(key=f"ai_site_done:{m.id}:0", value='{"completed":true}'))
        statements = []
        def record(_conn, _cursor, statement, _params, _ctx, _many):
            statements.append(statement)
        async with self.db.sessions() as s:
            c = await s.get(AIConversation, cid)
            event.listen(self.db.engine.sync_engine, "before_cursor_execute", record)
            try: data = await service.messages(s, c)
            finally: event.remove(self.db.engine.sync_engine, "before_cursor_execute", record)
        self.assertEqual(len(data), 100); self.assertEqual(len(statements), 4)
        self.assertTrue(all("encrypted_data" not in sql for sql in statements))
        self.assertTrue(all(m["actions"][0]["completed"] and len(m["files"]) == len(m["images"]) == 1 for m in data))
        self.assertEqual(data[0]["text"], "Message 0")

    async def test_history_has_constant_queries_and_preserves_search_and_ownership(self):
        async with self.db.sessions.begin() as s:
            actor = await s.get(User, self.uid)
            for i in range(100):
                c = AIConversation(user_id=self.uid, channel="owner", specialist="owner"); s.add(c); await s.flush()
                s.add(AIMessage(conversation_id=c.id,role="client",source_hash=f"first-{i}",text=f"First {i}"))
                s.add(AppSetting(key=f"assistant_title:{c.id}",value=f"Title {i}"))
            foreign = AIConversation(user_id=self.other,channel="owner",specialist="owner"); s.add(foreign); await s.flush()
            s.add(AppSetting(key=f"assistant_title:{foreign.id}",value="Foreign private"))
        async def current(_): return actor
        app = FastAPI(); app.include_router(ai_router(self.db,self.settings,current))
        statements = []
        def record(_conn,_cursor,sql,_params,_ctx,_many): statements.append(sql)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://example.test") as client:
            event.listen(self.db.engine.sync_engine,"before_cursor_execute",record)
            try: r = await client.get("/api/web/admin/assistant/chats")
            finally: event.remove(self.db.engine.sync_engine,"before_cursor_execute",record)
            self.assertEqual(r.status_code,200,r.text); self.assertEqual(len(r.json()["items"]),100)
            self.assertEqual(len(statements),2); self.assertNotIn("Foreign",r.text)
            found = await client.get("/api/web/admin/assistant/chats",params={"q":"Title 99"})
            self.assertEqual([c["title"] for c in found.json()["items"]],["Title 99"])
            body = await client.get(f"/api/web/admin/assistant/chats/{foreign.id}")
            self.assertEqual(body.status_code,404)
