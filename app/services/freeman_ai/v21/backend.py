"""Server-side authority boundary for the v2.1 agent.

The model never supplies customer identity or a remote URL. The only key scopes
are a verified conversation owner or an exact link in that client's own turn.
"""
import json
import re
from sqlalchemy import select
from app.database.models import (User, Subscription, VlessCredential, RemnawaveServer,
    Payment, Tariff, AccessKey, AIMessage, AIEvent, Ticket, now)
from app.database.repositories import SQLRepository
from app.services.freeman_ai.security import URL, aware, fingerprint, decrypt
from .registry import key_link, apps, URLS

class Backend:
    def __init__(self,session,settings,conversation,history,*,verified_user=None):
        self.s,self.settings,self.conv,self.history=session,settings,conversation,history
        self.uid=verified_user if verified_user is not None else conversation.user_id
        self.repo=SQLRepository(session)
        self.handlers=dict(diagnostic_scope=self.diagnostic_scope,get_subscriptions=self.subscriptions,get_devices=self.devices,
            prepare_detach=self.detach,prepare_reissue=self.reissue,get_payment=self.payment,
            get_purchase_link=self.purchase,get_antig_link=self.antig,generate_qr=self.qr,handoff=self.handoff,
            knowledge=self.knowledge,catalog=self.catalog,application_links=self.applications,
            respond=self.respond,resource_link=self.resource)

    def supplied_links(self):
        return {u for m in self.history if m.role=='client'
                for u in URL.findall(m.text) if key_link(u)}

    async def verified_user(self):
        if not self.uid:return None
        user=await self.s.get(User,self.uid)
        if not user or user.status!='active':raise PermissionError('account_unavailable')
        if self.conv.identity_id:
            from app.database.models import ChannelIdentity
            from app.services.freeman_ai.identity import customer
            identity=await self.s.get(ChannelIdentity,self.conv.identity_id)
            verified=await customer(self.s,identity)
            if not verified or verified.id!=user.id:raise PermissionError('identity_binding_revoked')
        return user

    async def resolve(self,ref):
        if not isinstance(ref,str) or not re.fullmatch(r'key:[1-9][0-9]{0,9}',ref) or int(ref[4:])>2147483647:
            raise PermissionError('key_unavailable')
        row=await self.s.get(VlessCredential,int(ref[4:]))
        if not row or row.status=='deleted' or not key_link(row.subscription_url):
            raise PermissionError('key_unavailable')
        user=await self.verified_user()
        permitted=user and row.user_id==user.id
        if not permitted:
            proof=await self.repo.setting(f'agent_v21_proof:{self.conv.id}:{row.id}')
            permitted=row.subscription_url in self.supplied_links() or bool(proof and decrypt(self.settings,proof)==fingerprint(self.settings,row.subscription_url))
        if not permitted:raise PermissionError('key_scope_required')
        sub=await self.s.get(Subscription,row.subscription_id)
        if not sub or await self.repo.setting(f'subscription_removed:{sub.id}')=='1':
            raise PermissionError('key_unavailable')
        return row

    async def subscriptions(self,args,state):
        from app.services.subscription_labels import current
        from app.services.vless_hwid import cache_state
        from sqlalchemy import or_

        user=await self.verified_user();links=self.supplied_links()
        query=select(VlessCredential).where(VlessCredential.status!='deleted')
        conditions=[]
        if user:conditions.append(VlessCredential.user_id==user.id)
        if links:conditions.append(VlessCredential.subscription_url.in_(links))
        if not conditions:return dict(subscriptions=[],identity_verified=False,key_required=True,actions=[])
        rows=list(await self.s.scalars(query.where(or_(*conditions)).order_by(VlessCredential.created_at.desc()).limit(40)))
        out=[]
        for c in rows:
            try:c=await self.resolve('key:'+str(c.id))
            except PermissionError:continue
            sub=await self.s.get(Subscription,c.subscription_id)
            tariff=await self.s.get(Tariff,sub.tariff_id) if sub.tariff_id else None
            key=await self.s.get(AccessKey,c.access_key_id) if c.access_key_id else None
            name,_=await current(self.repo,sub,key)
            device_count,_=await cache_state(self.s,sub.id)
            out.append(dict(key_ref='key:'+str(c.id),key_number=c.access_key_id,
                name=name,purchased_at=aware(sub.created_at).isoformat(),
                tariff=dict(months=tariff.months if tariff else None,devices=sub.device_limit),
                expires_at=aware(sub.expires_at).isoformat(),expired=aware(sub.expires_at)<=now(),
                device_limit=sub.device_limit,device_count=device_count,
                provider_status=c.status,source='database_not_live_panel'))
        actions=[]
        if self.s.info.get('agent_sessions'):
            from .interactions import command
            for item in out:
                number=item['key_number'] or item['key_ref'][4:]
                label=f'№{number} · {item["name"]}'
                actions.append(await command(self,int(item['key_ref'][4:]),'key',label[:115]))
        return dict(subscriptions=out,identity_verified=bool(user),scope='only_returned_keys',actions=actions)

    async def panel(self,c):
        from app.services.remnawave import RemnawaveService
        p=await self.s.get(RemnawaveServer,c.server_id)
        if not p or p.status=='deleted':raise ValueError('panel_unavailable')
        return RemnawaveService(self.repo,self.settings).api(p)

    def device_ref(self,c,hwid):
        return fingerprint(self.settings,f'agent-device:{c.id}:{hwid}')[:24]

    async def device_data(self,c):
        from app.services.vless_hwid import parse_devices
        api=await self.panel(c)
        rows=parse_devices(await api.devices(c.remote_user_uuid),c.remote_user_uuid)
        return [(self.device_ref(c,d['hwid']),d) for d in rows]

    async def devices(self,args,state):
        c=await self.resolve(args['key_ref'])
        if state is not None:
            state.selected_key=c.id
        rows=await self.device_data(c)
        from .interactions import device_actions
        actions=await device_actions(self,c,rows)
        return dict(key_ref=args['key_ref'],source='live_panel',actions=actions,devices=[dict(
            device_ref=ref,name=d.get('deviceModel') or 'Устройство',os=d.get('platform'),
            application=d.get('userAgent'),registered_at=d.get('createdAt'),last_activity=d.get('updatedAt')) for ref,d in rows])

    async def propose(self,c,kind,refs):
        from .rollout import mutations_enabled
        if not await mutations_enabled(self.s):
            return dict(
                completed=False, reason='key_mutations_disabled',
                guidance='Изменение из чата пока недоступно. Отвязать устройство можно в разделе «Мои ключи». С перевыпуском поможет оператор.',
            )
        from .actions import ActionService
        from .persistence import DurableRepository, PanelAdapter
        # Uses independent committed journal sessions; model transaction cannot
        # accidentally roll back the fact that a remote operation was started.
        sessions=self.s.info.get('agent_sessions')
        if sessions is None:raise ValueError('durable_journal_unconfigured')
        repo=DurableRepository(sessions,self.settings)
        panel=PanelAdapter(sessions,self.settings,self.conv.id,self.history,self.uid)
        token=await ActionService(repo,panel).prepare(str(self.conv.id),c.id,kind,refs)
        return dict(confirmation_required=True,operation=token,actions=[dict(
            type='agent_action',token=token,label='Перевыпустить ключ' if kind=='reissue' else 'Подтвердить отвязку',
            warning='Старая ссылка перестанет работать на всех устройствах. Срок, тариф и лимит сохранятся.' if kind=='reissue' else 'Будут отвязаны только выбранные подключения.',
            cancel_label='Отмена')])

    async def detach(self,args,state):
        c=await self.resolve(args['key_ref']);refs=args['device_refs']
        if not isinstance(refs,list) or not 1<=len(refs)<=100 or any(not isinstance(x,str) for x in refs):raise ValueError('invalid_device_selection')
        return await self.propose(c,'detach',refs)

    async def reissue(self,args,state):
        return await self.propose(await self.resolve(args['key_ref']),'reissue',[])

    async def payment(self, args, state):
        from decimal import Decimal, InvalidOperation

        from app.services.topups import YooKassa

        user = await self.verified_user()
        if not user:
            raise PermissionError('payment_account_verification_required')
        ref = args['payment_ref']
        query = select(Payment).where(Payment.user_id == user.id)
        if ref:
            if not re.fullmatch(r'[1-9][0-9]{0,9}', ref) or int(ref) > 2147483647:
                raise ValueError('invalid_payment_reference')
            query = query.where(Payment.id == int(ref))
        rows = list(await self.s.scalars(query.order_by(Payment.id.desc()).limit(5)))
        output = []
        for payment in rows:
            linked = await self.s.scalar(
                select(VlessCredential).where(
                    VlessCredential.subscription_id == payment.subscription_id,
                    VlessCredential.user_id == user.id,
                    VlessCredential.status != 'deleted',
                )
            ) if payment.subscription_id else None
            item = dict(
                payment_ref=str(payment.id), status=payment.status, amount_kopecks=payment.amount,
                currency='RUB', key_ref=f'key:{linked.id}' if linked else None,
                created_at=aware(payment.created_at).isoformat(), source='local_payment_state',
            )
            if ref and payment.provider_id and payment.status.startswith('order_'):
                try:
                    remote = await YooKassa(self.settings).get(payment.provider_id)
                    amount, metadata = remote.get('amount', {}), remote.get('metadata', {})
                    if (
                        remote.get('id') != payment.provider_id
                        or amount.get('currency') != 'RUB'
                        or Decimal(amount.get('value', '0')) * 100 != payment.amount
                        or metadata.get('freeman_payment') != str(payment.id)
                        or metadata.get('freeman_user') != str(user.id)
                        or remote.get('test') is not self.settings.yookassa_test_mode
                    ):
                        raise ValueError('unmatched_provider_payment')
                    item['provider_status'] = remote.get('status')
                    item['provider_paid'] = remote.get('paid') is True and remote.get('status') == 'succeeded'
                    item['source'] = 'verified_yookassa_and_local_state'
                except (ValueError, InvalidOperation, AttributeError, TypeError):
                    item['provider_status'] = 'not_verified'
            output.append(item)
        return dict(
            payments=output, charge_performed=False,
            guidance='Подтверждение списания провайдером не означает выдачу или продление. Ключ и срок проверяй отдельно.',
        )

    async def purchase(self,args,state):
        if args['renewal']:
            c=await self.resolve(args['key_ref'])
            if state is not None:
                state.selected_key=c.id
            from .interactions import command
            a=await command(self,c.id,'renew','Продлить подписку')
            return dict(selected_key=args['key_ref'],actions=[dict(type='link',label='Продлить подписку',url=self.settings.web_base_url.rstrip('/')+'/support/renew#'+a['token'])],charge_performed=False)
        return dict(actions=[dict(type='link',label='🤖 Открыть новый бот',url=URLS['bot']),dict(type='link',label='🌐 Купить на сайте',url=URLS['buy'])])

    async def qr(self,args,state):
        from .interactions import command
        c=await self.resolve(args['key_ref'])
        action=await command(self,c.id,'qr','QR подписки')
        return dict(actions=[dict(type='agent_image',token=action['token'],label='QR подписки')])

    async def diagnostic_scope(self,args,state):
        from .interactions import command
        from .support_content import SCOPE_OPTIONS
        return dict(actions=[await command(self,None,'diagnostic_scope',label,choice=i,generation=self.conv.generation)
                             for i,label in enumerate(SCOPE_OPTIONS)])

    async def region_action(self):
        if self.conv.channel in ('telegram','support_agent_telegram'):
            from .interactions import command
            return await command(self,None,'region','Как сменить регион')
        return dict(type='material',label='Как сменить регион',material='appstore-region')

    async def antig(self,args,state):
        from app.web.key_antig import addon_active, key_context
        from app.services.bot_antig_price import enabled
        from .registry import cabinet_key_url

        c=await self.resolve(args['key_ref'])
        user=await self.verified_user()
        if not user or user.id!=c.user_id or not c.access_key_id:
            return dict(available=False,reason='account_verification_required')
        if not await enabled(self.repo):
            return dict(available=False,reason='disabled')
        try:
            _,_,key,sub,_,_=await key_context(self.s,self.settings,user,c.access_key_id)
        except (PermissionError,ValueError):
            return dict(available=False,reason='subscription_unavailable')
        present=await addon_active(self.repo,sub)
        if present is not False:
            return dict(available=False,antig_active=present)
        return dict(available=True,charge_performed=False,actions=[
            dict(type='link',label='Добавить антиглушилку',url=cabinet_key_url(key.id,'antig')),
        ])

    async def handoff(self,args,state):
        reason=args['reason'][:300];summary=args['summary'][:3000]
        from app.services.freeman_ai.service import lock_ticket
        await lock_ticket(self.s,self.conv)
        state.request_operator(reason)
        await self.repo.set_setting(f'agent_v21_waiting:{self.conv.id}','1')
        self.conv.summary=summary
        if self.conv.ticket_id:
            t=await self.s.get(Ticket,self.conv.ticket_id)
            if t and t.status!='closed':t.status='waiting_admin';t.updated_at=now()
        # AIConversation is already a real row in the operator inbox. Business
        # guests stay conversations without inventing a permanent Freeman user.
        await self.repo.set_setting(f'agent_v21_queue:{self.conv.id}',json.dumps(dict(
            reason=reason,summary=summary,created_at=now().isoformat(),status='waiting_operator'),ensure_ascii=False))
        self.s.add(AIEvent(conversation_id=self.conv.id,kind='handoff',name='waiting_operator'))
        return dict(conversation_id=self.conv.id,ticket_id=self.conv.ticket_id,status='waiting_operator',ai_continues_until_operator_reply=True)

    async def knowledge(self,args,state):
        from .knowledge import retrieve
        return await retrieve(self.s,args['query'],context=args.get('context',''))

    async def catalog(self,args,state):
        from app.services.bot_widget_demo import catalogue
        from app.services.bot_antig_price import enabled
        from types import SimpleNamespace
        user=await self.verified_user()
        rows,_=await catalogue(self.repo,user or SimpleNamespace(id=0),admin=not bool(user))
        anti=await enabled(self.repo)
        return dict(antig_available=anti,tariffs=[dict(devices=d,months=m,amount_kopecks=p,
            with_antig_kopecks=p+extra if anti else None) for d,m,p,_,extra in rows])

    async def applications(self,args,state):
        rows=apps(args['platform'])
        if args['application']:rows=[r for r in rows if r['name'].casefold()==args['application'].casefold()]
        return dict(apps=rows,availability='depends_on_customer_store_region',actions=[dict(type='link',label='Скачать '+r['name'],url=r['url']) for r in rows])

    async def remember(self,args,state):
        allowed={'device_model','os','application','application_version','network','expected_answer','financial_request'}
        for k in allowed:
            v=args[k]
            if not isinstance(v,str) or len(v)>500:raise ValueError('invalid_state_update')
            if v:setattr(state,k,v)
        if args['check_name']:state.record_check(args['check_name'],args['check_scope'],args['check_result'])
        return dict(saved=True,completed_checks=state.checks,financial_request=state.financial_request)

    async def respond(self,args,state):
        await self.remember(args,state)
        if args['new_symptom'] and args['new_symptom'] not in state.symptoms:
            state.symptoms=(state.symptoms+[args['new_symptom']])[-12:]
        if args['technical_resolved']:state.resolve_technical()
        if state.phase=='clarification':state.phase='waiting_customer'
        return dict(final_text=args['text'])

    async def resource(self,args,state):
        resource=args['resource']
        if resource.startswith('setup_'):
            from .support_content import setup_guide
            return setup_guide(resource.removeprefix('setup_'))
        if resource not in ('speed','tv'):raise ValueError('unknown_resource')
        return dict(actions=[dict(type='link',label='Проверить скорость' if resource=='speed' else 'Передача на TV',url=URLS[resource])])
