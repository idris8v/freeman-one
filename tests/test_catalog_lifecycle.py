"""History-safe retirement using real admin APIs, isolated PostgreSQL only."""
import asyncio
import json
import os
import unittest
from datetime import timedelta
from sqlalchemy import text

import httpx
from fastapi import FastAPI
from test_ui_functional import UIFunctional

from app.database.models import AppSetting, Subscription, Tariff, ProxyPlan, User, now
from app.database.repositories import SQLRepository
from app.services.proxy_control import plans
from app.web.catalog import catalog_router
from app.web.proxy_control import proxy_control_router


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "isolated PostgreSQL")
class CatalogLifecycle(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = UIFunctional.asyncSetUp
    asyncTearDown = UIFunctional.asyncTearDown

    async def client(self):
        helper = await UIFunctional.client(self)
        await helper.aclose()
        async with self.db.sessions() as s: actor = await s.get(User,self.uid)
        async def current(_): return actor
        app=FastAPI();app.include_router(catalog_router(self.db,self.settings,current));app.include_router(proxy_control_router(self.db,self.settings,current))
        client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://example.test")
        self.addAsyncCleanup(client.aclose);return client

    async def test_unused_plans_removed_with_idempotent_tombstone(self):
        client=await self.client()
        async with self.db.sessions.begin() as s:
            t=Tariff(months=5,price=10000,device_limit=2);p=ProxyPlan(name="Unused",price=1000);s.add_all([t,p]);await s.flush();tid,pid=t.id,p.id
        for url in [f"/api/web/admin/catalog/vpn/{tid}",f"/api/web/admin/proxy/control/plan/{pid}"]:
            replies=await asyncio.gather(*(client.delete(url,params={"version":0}) for _ in range(2)))
            self.assertTrue(all(r.status_code==200 and r.json()["deleted"] for r in replies))
        listing=(await client.get('/api/web/admin/catalog')).json()
        self.assertFalse(any(t['id']==tid for t in listing['vpn']));self.assertFalse(any(p['id']==pid for p in listing['proxy']))
        async with self.db.sessions() as s:
            repo=SQLRepository(s)
            self.assertIsNone(await s.get(Tariff,tid));self.assertIsNone(await s.get(ProxyPlan,pid))
            self.assertEqual(await repo.setting(f'tariff_enabled:id{tid}'),'0')
            self.assertEqual(await plans(repo),[]) # deleting the final plan must not seed another

    async def test_referenced_and_pending_plans_archive_and_preserve_terms(self):
        client=await self.client()
        async with self.db.sessions.begin() as s:
            t=Tariff(months=5,price=10000,device_limit=2);p=ProxyPlan(name="Pending",price=1000);s.add_all([t,p]);await s.flush();tid,pid=t.id,p.id
            sub=Subscription(user_id=self.uid,kind='vpn',tariff_id=tid,device_limit=2,expires_at=now()+timedelta(days=50));s.add(sub);await s.flush();sid=sub.id;expiry=sub.expires_at
            s.add(AppSetting(key='proxy_order:123',value=json.dumps({'plan_id':pid,'price':1000,'status':'pending'})))
        for url in [f"/api/web/admin/catalog/vpn/{tid}",f"/api/web/admin/proxy/control/plan/{pid}"]:
            conflict=await client.delete(url,params={'version':99});self.assertEqual(conflict.status_code,409)
            r=await client.delete(url,params={'version':0});self.assertEqual(r.status_code,200,r.text);self.assertTrue(r.json()['archived']);self.assertFalse(r.json()['deleted'])
        listing=(await client.get('/api/web/admin/catalog')).json()
        for kind,identifier in [('vpn',tid),('proxy',pid)]:
            row=next(v for v in listing[kind] if v['id']==identifier);self.assertTrue(row['archived']);self.assertFalse(row['enabled'])
        async with self.db.sessions() as s:
            self.assertEqual((await s.get(Subscription,sid)).expires_at,expiry)
            self.assertEqual((await s.get(Tariff,tid)).price,10000)
            self.assertEqual(json.loads(await SQLRepository(s).setting('proxy_order:123'))['price'],1000)

    async def test_concurrent_edit_and_retire_cannot_reenable_or_lose_version(self):
        client=await self.client()
        for kind in ['vpn','proxy']:
            async with self.db.sessions.begin() as s:
                item=Tariff(months=5,price=10000,device_limit=2) if kind=='vpn' else ProxyPlan(name='Race',price=10000)
                s.add(item);await s.flush();identifier=item.id
            edit='/api/web/admin/catalog' if kind=='vpn' else '/api/web/admin/proxy/control/plan'
            delete=f'/api/web/admin/catalog/vpn/{identifier}' if kind=='vpn' else f'/api/web/admin/proxy/control/plan/{identifier}'
            payload=dict(kind=kind,id=identifier,version=0,name='Edited',price='110',months=5,devices=2,confirmed=True,request_id='catalog-race-'+kind,enabled=True)
            responses=await asyncio.gather(client.post(edit,json=payload),client.delete(delete,params={'version':0}))
            self.assertEqual(sorted(r.status_code for r in responses),[200,409],[(r.status_code,r.text)for r in responses])
            async with self.db.sessions() as s:
                meta=json.loads(await SQLRepository(s).setting(f'catalog_{kind}:{identifier}'))
                self.assertEqual(meta['version'],1)
                if responses[1].status_code==200:
                    self.assertTrue(meta['deleted'])

    async def test_archived_unused_plan_is_purged_but_stale_edit_is_rejected(self):
        client=await self.client()
        for kind in ('vpn','proxy'):
            async with self.db.sessions.begin() as s:
                item=Tariff(months=2,price=10000,device_limit=2) if kind=='vpn' else ProxyPlan(name='Old archive',price=10000)
                s.add(item);await s.flush();identifier=item.id
                await SQLRepository(s).set_setting(f'catalog_{kind}:{identifier}',json.dumps(dict(archived=True,version=3)))
            url=f'/api/web/admin/catalog/vpn/{identifier}' if kind=='vpn' else f'/api/web/admin/proxy/control/plan/{identifier}'
            result=await client.delete(url,params=dict(version=3));self.assertEqual(result.status_code,200,result.text);self.assertTrue(result.json()['purged'])
            edit='/api/web/admin/catalog' if kind=='vpn' else '/api/web/admin/proxy/control/plan'
            payload=dict(kind=kind,id=identifier,version=3,name='Stale edit',price='100',months=2,devices=2,confirmed=True,request_id='deleted-edit-'+kind)
            result=await client.post(edit,json=payload);self.assertEqual(result.status_code,409,result.text)

    async def test_frozen_alias_and_all_durable_order_references_are_retained(self):
        client=await self.client()
        for prefix,scalar in [('legacy_tariff:',True),('guest_tariff:',True),('auto_renew_tariff:',True),('stars_order:',False),('balance_purchase:',False),('manual_sale:',False),('public_promotion:',False)]:
            async with self.db.sessions.begin() as s:
                tariff=Tariff(months=2,price=10000,device_limit=2);s.add(tariff);await s.flush();tid=tariff.id
                payload=str(tid) if scalar else json.dumps(dict(product='vpn',plan_ids=[tid])) if prefix=='public_promotion:' else json.dumps(dict(tariff_id=tid))
                await SQLRepository(s).set_setting(prefix+str(tid),payload)
            result=await client.delete(f'/api/web/admin/catalog/vpn/{tid}',params=dict(version=0));self.assertEqual(result.status_code,200,result.text)
            self.assertFalse(result.json()['deleted'],prefix);self.assertTrue(result.json()['reason'])
            async with self.db.sessions() as s:
                tariff=await s.get(Tariff,tid);self.assertIsNotNone(tariff);self.assertEqual(await SQLRepository(s).tariff_setting(tariff,'tariff_enabled'),'0')

    async def test_malformed_history_cannot_authorize_physical_deletion(self):
        client=await self.client()
        async with self.db.sessions.begin() as s:
            t=Tariff(months=5,price=10000);s.add(t);await s.flush();tid=t.id
            await SQLRepository(s).set_setting('order:unknown-legacy','not-json')
        result=await client.delete(f'/api/web/admin/catalog/vpn/{tid}',params=dict(version=0))
        self.assertEqual(result.status_code,200,result.text);self.assertFalse(result.json()['deleted'])

    async def test_concurrent_new_order_claim_prevents_physical_deletion(self):
        client=await self.client();claimed=asyncio.Event();release=asyncio.Event()
        async with self.db.sessions.begin() as s:
            t=Tariff(months=5,price=10000);s.add(t);await s.flush();tid=t.id
        async def stage():
            async with self.db.sessions.begin() as s:
                repo=SQLRepository(s);tariff=await repo.lock_tariff(await s.get(Tariff,tid));self.assertIsNotNone(tariff)
                claimed.set();await release.wait()
                await repo.set_setting('order:staging',json.dumps(dict(tariff_id=tid)))
        staging=asyncio.create_task(stage());await claimed.wait()
        deleting=asyncio.create_task(client.delete(f'/api/web/admin/catalog/vpn/{tid}',params=dict(version=0)))
        try:
            async with asyncio.timeout(5):
                while True:
                    async with self.db.sessions() as s:
                        waiting=await s.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND wait_event_type='Lock' AND query LIKE '%pg_advisory_xact_lock(%' AND state='active'"))
                    if waiting:break
                    await asyncio.sleep(.01)
            self.assertFalse(deleting.done());release.set();await staging
            response=await deleting;self.assertEqual(response.status_code,200,response.text);self.assertFalse(response.json()['deleted'])
            async with self.db.sessions() as s:self.assertIsNotNone(await s.get(Tariff,tid))
        finally:
            release.set();await asyncio.gather(staging,deleting,return_exceptions=True)

    async def test_customer_claim_does_not_wait_behind_catalog_writer(self):
        from app.services.customer_identity import lock_nonce
        async with self.db.sessions.begin() as s:
            t=Tariff(months=5,price=10000);s.add(t);await s.flush();tid=t.id
        async with self.db.sessions.begin() as writer:
            await lock_nonce(writer,f'catalog:vpn:{tid}')
            async with self.db.sessions.begin() as reader:
                repo=SQLRepository(reader)
                result=await asyncio.wait_for(repo.lock_tariff(await reader.get(Tariff,tid)),5)
                self.assertIsNone(result)
        async with self.db.sessions.begin() as reader:
            repo=SQLRepository(reader)
            self.assertIsNotNone(await repo.lock_tariff(await reader.get(Tariff,tid)))
