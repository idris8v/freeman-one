from datetime import timedelta
import base64
import pytest
from sqlalchemy import select, func
from test_bot import database as database, register
from app.config.settings import Settings
from app.database.models import BalanceTransaction, FunnelEvent, Subscription, ProxyServer, ProxyCredential, VpnServer, VpnCredential, AccessKey, now
from app.database.repositories import SQLRepository
from app.services.admin import AdminService
from app.services.control import ControlService
from app.services.proxies import ProxyService
from app.services.vpn import VpnService


def config():
    from cryptography.fernet import Fernet
    return Settings(_env_file=None,admin_ids='8066410491',encryption_key=Fernet.generate_key().decode())




async def test_funnel_counts_each_user_once(database):
    owner=await register(database,8066410491);client=await register(database,2)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        await repo.track_funnel(client.id,'start')
        await repo.track_funnel(client.id,'start')
        await repo.track_funnel(client.id,'plans')
        screen=await ControlService(repo,config()).render('admin:x:funnel:30',owner)
        assert 'Запустили бота: 1' in screen.text
        assert 'Открыли тарифы: 1' in screen.text
        assert await session.scalar(select(func.count()).select_from(FunnelEvent))==2






async def test_withdrawal_default_is_one_thousand_rubles(database):
    client=await register(database,2)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        await repo.add(BalanceTransaction(user_id=client.id,type='credit',amount=99999,description='test'))
        screen=await ControlService(repo,config()).render('x:withdraw',client)
        assert '1 000 ₽' in screen.text




async def test_telemt_wizard_completes_all_seven_steps(database, monkeypatch):
    owner=await register(database,8066410491);settings=config()
    async def health(_self): return {'ready':True}
    monkeypatch.setattr('app.services.telemt.TelemtClient.health',health)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=ProxyService(repo,settings)
        first=await service.render('admin:p:input:server',owner)
        assert 'Шаг 1 из 7' in first.text
        values=['Финляндия 1','https://api.example:9091','vpn','10','2','10','secret-header']
        for index,value in enumerate(values[:-1],start=2):
            screen=await service.input('admin:p:input:server',owner,value)
            assert screen.keep_input is True
            assert f'Шаг {index} из 7' in screen.text
            assert isinstance(screen.rows[0],list)
        result=await service.input('admin:p:input:server',owner,values[-1])
        assert 'Финляндия 1' in result.text and result.keep_input is False
        assert await session.scalar(select(func.count()).select_from(ProxyServer))==1


async def test_new_telemt_server_issues_preserved_paid_access(database,monkeypatch):
    owner=await register(database,8066410491);client=await register(database,2);settings=config();seen={}
    async def health(_self): return {'ready':True}
    async def create(_self,username,**values):
        seen.update(values)
        return {'user':{'username':username,'links':{'secure':['tg://proxy?server=paid.example&port=443&secret='+'cd'*16]}}}
    monkeypatch.setattr('app.services.telemt.TelemtClient.health',health)
    monkeypatch.setattr('app.services.telemt.TelemtClient.create_user',create)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=ProxyService(repo,settings)
        await repo.set_setting(f'proxy_paid_until:{client.id}',(now()+timedelta(days=30)).isoformat())
        await service.input('admin:p:input:server',owner,'Оплаченный | https://api.paid.example:9091 | paid | 10 | 2 | 2 | auth-paid')
        credential=await session.scalar(select(ProxyCredential).where(ProxyCredential.user_id==client.id))
        assert credential and seen['max_unique_ips']==2 and seen['max_tcp_conns']==2


async def test_proxy_admin_has_rich_stats_and_full_key_controls(database,monkeypatch):
    owner=await register(database,8066410491);client=await register(database,2);settings=config();calls=[]
    row={'username':'freeman_2','enabled':True,'current_connections':1,'active_unique_ips':1,'total_octets':4096,'links':{'secure':['tg://proxy?server=managed.example&port=443&secret='+'ef'*16]}}
    async def health(_self): return {'ready':True}
    async def create(_self,username,**values): return {'user':dict(row)}
    async def users(_self): return [dict(row)]
    async def one(_self,username): return dict(row)
    async def enable(_self,username): calls.append(('enable',username));return {}
    async def disable(_self,username): calls.append(('disable',username));return {}
    async def remove(_self,username): calls.append(('delete',username));return {}
    monkeypatch.setattr('app.services.telemt.TelemtClient.health',health)
    monkeypatch.setattr('app.services.telemt.TelemtClient.create_user',create)
    monkeypatch.setattr('app.services.telemt.TelemtClient.users',users)
    monkeypatch.setattr('app.services.telemt.TelemtClient.user',one)
    monkeypatch.setattr('app.services.telemt.TelemtClient.enable_user',enable)
    monkeypatch.setattr('app.services.telemt.TelemtClient.disable_user',disable)
    monkeypatch.setattr('app.services.telemt.TelemtClient.delete_user',remove)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=ProxyService(repo,settings)
        await repo.set_setting(f'proxy_paid_until:{client.id}',(now()+timedelta(days=30)).isoformat())
        await service.input('admin:p:input:server',owner,'Managed | https://api.managed.example:9091 | paid | 10 | 2 | 2 | auth-managed')
        server=await session.scalar(select(ProxyServer));credential=await session.scalar(select(ProxyCredential))
        card=await service.render(f'admin:p:server:{server.id}',owner)
        assert card.rich and 'Выдача: включена' in card.text
        assert any(target==f'admin:p:users:{server.id}:0' for row_buttons in card.rows for _,target in row_buttons)
        users_screen=await service.render(f'admin:p:users:{server.id}:0',owner)
        assert users_screen.rich and str(client.telegram_id) in users_screen.text
        key_screen=await service.render(f'admin:p:cred:{credential.id}',owner)
        assert key_screen.rich and 'Соединений' in key_screen.text
        await service.render(f'admin:p:credtoggle:{credential.id}',owner)
        assert credential.status=='disabled' and calls[-1][0]=='disable'
        await service.render(f'admin:p:credtoggle:{credential.id}',owner)
        assert credential.status=='active' and calls[-1][0]=='enable'
        diagnose=await service.render(f'admin:p:diagnose:{server.id}',owner)
        assert 'read-only file system' in diagnose.text
        await service.render(f'admin:p:creddelete:{credential.id}',owner)
        assert credential.status=='deleted' and calls[-1][0]=='delete'


async def test_proxy_server_fields_are_edited_independently(database,monkeypatch):
    owner=await register(database,8066410491);settings=config()
    async def health(_self): return {'ready':True}
    monkeypatch.setattr('app.services.telemt.TelemtClient.health',health)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=ProxyService(repo,settings)
        server=await service.save_server('Первый | https://api.fields.example:9091 | paid | 10 | 2 | 2 | auth-one')
        card=await service.render(f'admin:p:server:{server.id}',owner)
        assert any(target==f'admin:p:settings:{server.id}' for row in card.rows for _,target in row)
        settings_screen=await service.render(f'admin:p:settings:{server.id}',owner)
        assert 'Настройки сервера' in settings_screen.text and 'Отдельная покупка' in settings_screen.text
        await service.input(f'admin:p:input:servername:{server.id}',owner,'Клиенты VPN')
        await service.input(f'admin:p:input:serverpriority:{server.id}',owner,'25')
        await service.input(f'admin:p:input:serverlimits:{server.id}',owner,'3 | 7')
        await service.render(f'admin:p:audienceapply:{server.id}:vpn',owner)
        assert (server.name,server.priority,server.unique_ip_limit,server.tcp_connection_limit,server.audience)==('Клиенты VPN',25,3,7,'vpn')
        await service.input(f'admin:p:input:serverapi:{server.id}',owner,'https://api.changed.example:9443 | auth-two')
        assert server.api_url=='https://api.changed.example:9443' and 'auth-two' not in server.auth


async def test_proxy_read_only_diagnostic_can_retry_issuance(database,monkeypatch):
    from app.services.telemt import TelemtError
    owner=await register(database,8066410491);client=await register(database,2);settings=config();state={'blocked':True}
    async def health(_self): return {'ready':True}
    async def users(_self): return []
    async def create(_self,username,**values):
        if state['blocked']: raise TelemtError('Telemt API: failed to write config: read-only file system',500)
        return {'user':{'username':username,'links':{'secure':['tg://proxy?server=retry.example&port=443&secret='+'aa'*16]}}}
    monkeypatch.setattr('app.services.telemt.TelemtClient.health',health)
    monkeypatch.setattr('app.services.telemt.TelemtClient.users',users)
    monkeypatch.setattr('app.services.telemt.TelemtClient.create_user',create)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=ProxyService(repo,settings)
        await repo.set_setting(f'proxy_paid_until:{client.id}',(now()+timedelta(days=30)).isoformat())
        await service.input('admin:p:input:server',owner,'Retry | https://api.retry.example:9091 | paid | 10 | 2 | 2 | auth-retry')
        server=await session.scalar(select(ProxyServer))
        card=await service.render(f'admin:p:server:{server.id}',owner)
        assert 'read-only file system' in card.text
        assert any(target==f'admin:p:retry:{server.id}' for row in card.rows for _,target in row)
        state['blocked']=False
        result=await service.render(f'admin:p:retry:{server.id}',owner)
        assert 'Активные ключи' in result.rich.model_dump_json()
        assert await session.scalar(select(ProxyCredential).where(ProxyCredential.user_id==client.id))
        assert await repo.setting(f'proxy_issue_error:{server.id}')==''


async def test_deleted_telemt_url_is_restored_instead_of_rejected(database,monkeypatch):
    owner=await register(database,8066410491);settings=config()
    async def health(_self): return {'ready':True}
    monkeypatch.setattr('app.services.telemt.TelemtClient.health',health)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=ProxyService(repo,settings)
        first=await service.save_server('Старый | https://api.restore.example:9091 | paid | 10 | 2 | 2 | old-auth')
        first.status='deleted'
        restored=await service.save_server('Новый | https://api.restore.example:9091 | all | 5 | 3 | 4 | new-auth')
        assert restored.id==first.id and restored.status=='active' and restored.name=='Новый'
        assert restored.audience=='all' and restored.unique_ip_limit==3
        assert await session.scalar(select(func.count()).select_from(ProxyServer))==1


async def test_admin_can_move_one_and_all_proxy_credentials(database,monkeypatch):
    owner=await register(database,8066410491);client=await register(database,2);settings=config();disabled=[]
    async def health(_self): return {'ready':True}
    async def create(self,username,**values):
        host='target.example' if 'target' in self.base_url else 'source.example'
        return {'user':{'username':username,'links':{'secure':[f'tg://proxy?server={host}&port=443&secret='+('ab'*16)]}}}
    async def patch(self,username,**values): return await create(self,username,**values)
    async def disable(self,username): disabled.append((self.base_url,username));return {}
    monkeypatch.setattr('app.services.telemt.TelemtClient.health',health)
    monkeypatch.setattr('app.services.telemt.TelemtClient.create_user',create)
    monkeypatch.setattr('app.services.telemt.TelemtClient.patch_user',patch)
    monkeypatch.setattr('app.services.telemt.TelemtClient.disable_user',disable)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=ProxyService(repo,settings)
        await repo.set_setting(f'proxy_paid_until:{client.id}',(now()+timedelta(days=30)).isoformat())
        source=await service.save_server('Источник | https://api.source.example:9091 | paid | 10 | 2 | 2 | source-auth')
        credential,_=await service.provision(source,client,force_kind='paid')
        target=await service.save_server('Резерв | https://api.target.example:9091 | paid | 20 | 2 | 2 | target-auth')
        picker=await service.render(f'admin:p:move:{credential.id}',owner)
        assert any(value==f'admin:p:moveapply:{credential.id}:{target.id}' for row in picker.rows for _,value in row)
        result=await service.render(f'admin:p:moveapply:{credential.id}:{target.id}',owner)
        assert credential.status=='migrated' and disabled[-1][0]==source.api_url
        moved=await session.scalar(select(ProxyCredential).where(ProxyCredential.server_id==target.id,ProxyCredential.user_id==client.id))
        assert moved and moved.status=='active' and 'Старый доступ отозван' in result.text




async def test_screen_accepts_gif_and_video(database):
    owner=await register(database,8066410491);settings=config()
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=ControlService(repo,settings)
        await service.input('admin:x:input:media:home',owner,'',None,('animation','gif-file-id'))
        assert await repo.setting('media_type:home')=='animation'
        assert await repo.setting('media_file:home')=='gif-file-id'
        await service.input('admin:x:input:media:home',owner,'—',None,None)
        assert await repo.setting('media_type:home')==''


async def test_vpn_server_wizard_provisions_real_amnezia_config(database,monkeypatch):
    owner=await register(database,8066410491);client=await register(database,2);settings=config();updates=[]
    async def health(_self): return {'status':'ok'}
    async def server(_self): return {'protocols':['amneziawg3'],'region':'FI'}
    async def load(_self): return {'cpuPercent':12,'memoryPercent':23,'diskPercent':34}
    async def clients(_self,skip=0,limit=100): return {'total':0,'items':[]}
    async def create(_self,name,protocol,expires_at):
        assert name.startswith('freeman_2_') and protocol=='amneziawg3' and expires_at>0
        return {'client':{'id':'public_key_1','config':'vpn://working-config','protocol':'amneziawg3'}}
    async def qr(_self,config): return {'total':1,'items':['data:image/png;base64,'+base64.b64encode(b'png').decode()]}
    async def update(_self,client_id,protocol,**values): updates.append((client_id,protocol,values));return {'message':'ok'}
    async def delete(_self,client_id,protocol): return {'message':'ok'}
    monkeypatch.setattr('app.services.amnezia_api.AmneziaApiClient.health',health)
    monkeypatch.setattr('app.services.amnezia_api.AmneziaApiClient.server',server)
    monkeypatch.setattr('app.services.amnezia_api.AmneziaApiClient.load',load)
    monkeypatch.setattr('app.services.amnezia_api.AmneziaApiClient.clients',clients)
    monkeypatch.setattr('app.services.amnezia_api.AmneziaApiClient.create_client',create)
    monkeypatch.setattr('app.services.amnezia_api.AmneziaApiClient.qr',qr)
    monkeypatch.setattr('app.services.amnezia_api.AmneziaApiClient.update_client',update)
    monkeypatch.setattr('app.services.amnezia_api.AmneziaApiClient.delete_client',delete)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=VpnService(repo,settings)
        await service.render('admin:v:input:server',owner)
        values=['FI 1','🇫🇮 Финляндия','https://fi.example','amneziawg3','10','500','a'*32]
        for index,value in enumerate(values[:-1],start=2):
            screen=await service.input('admin:v:input:server',owner,value)
            assert screen.keep_input and f'Шаг {index} из 7' in screen.text
        await service.input('admin:v:input:server',owner,values[-1])
        vpn_server=await session.scalar(select(VpnServer));assert vpn_server.health=='online'
        sub=await repo.add(Subscription(user_id=client.id,kind='paid',expires_at=now()+timedelta(days=30),device_limit=1))
        locations=await service.render(f'v:locations:{sub.id}',client)
        assert 'Финляндия' in locations.rows[0][0][0]
        result=await service.render(f'v:select:{sub.id}:{vpn_server.id}',client)
        assert result.photo and 'VPN-подключение' in result.text
        credential=await session.scalar(select(VpnCredential));key=await session.scalar(select(AccessKey))
        assert credential and 'working-config' not in credential.config
        assert key.mode=='amneziawg3' and await repo.setting(f'demo_file:{key.id}')==''
        await service.set_subscription_status(sub.id,'disabled')
        assert updates[-1][2]['status']=='disabled'
