"""Read-only live panel/node monitoring for the owner."""
import asyncio
from html import escape
from sqlalchemy import select
from app.database.models import RemnawaveServer, now
from app.services.screens import Screen
from app.services.document_rich import document_layout


def gauge(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        return 'Нет данных'
    filled = round(value / 10)
    return '▰' * filled + '▱' * (10-filled) + f' {value:.0f}%'


def state(node):
    if node.get('isDisabled') is True: return '⚪ Отключён'
    if node.get('isConnected') is False: return '🔴 Недоступен'
    if node.get('isConnected') is not True: return '⚪ Нет данных о связи'
    values = [node.get(k) for k in ('cpuUsage', 'memoryUsage')]
    values = [v for v in values if isinstance(v, (int,float)) and not isinstance(v,bool) and 0 <= v <= 100]
    if values and max(values) >= 90: return '🔴 Требует внимания'
    if values and max(values) >= 75: return '🟡 Под нагрузкой'
    return '🟢 На связи' if not values else '🟢 В норме'


def normalize(node):
    node = dict(node)
    system = node.get('system') or {}
    info, stats = system.get('info') or {}, system.get('stats') or {}
    total, used = info.get('memoryTotal'), stats.get('memoryUsed')
    if isinstance(total,(int,float)) and total>0 and isinstance(used,(int,float)):
        node['memoryUsage'] = used/total*100
    load = stats.get('loadAvg') or []
    cores = info.get('cpus')
    if load and isinstance(cores,(int,float)) and cores>0:
        node['loadText'] = f'{load[0]:.2f} / {cores} ядер (1 мин)'
    interface = stats.get('interface') or {}
    for field in ('rxBytesPerSec','txBytesPerSec'):
        value=interface.get(field)
        node[field]=f'{value/1024:.1f} КБ/с' if isinstance(value,(int,float)) else 'Нет данных'
    return node


async def read_panels(service, active_only=False):
    query = select(RemnawaveServer).order_by(RemnawaveServer.id)
    if active_only:
        query = query.where(RemnawaveServer.status == 'active')
    panels = list(await service.session.scalars(query))
    async def read(panel):
        try:
            data = await asyncio.wait_for(service.api(panel).nodes(), timeout=8)
            response = data.get('response', data)
            nodes = response if isinstance(response,list) else response.get('nodes', [])
            if not isinstance(nodes,list): raise ValueError('Invalid nodes')
            return panel, [normalize(n) for n in nodes if isinstance(n,dict)], True
        except Exception:
            return panel, [], False
    return await asyncio.gather(*(read(panel) for panel in panels))


async def status_screen(service, parts):
    results = await read_panels(service)
    panels = [panel for panel, _, _ in results]
    timestamp = now().strftime('%d.%m.%Y %H:%M UTC')
    entries = [(panel,n) for panel,nodes,ok in results for n in nodes]
    if parts[0] == 'status' and len(parts) >= 3:
        try: panel,node = entries[int(parts[2])]
        except (IndexError,ValueError):
            return Screen('Список изменился. Обновите статус серверов.', [[('← Назад','admin:r:home')]],structured=True)
        name = str(node.get('name') or 'Сервер')
        online = node.get('usersOnline')
        body = f'{state(node)}\nПанель: {panel.name}\nПроверено: {timestamp}\nПроцессор: {gauge(node.get("cpuUsage"))}\nПамять: {gauge(node.get("memoryUsage"))}\nСредняя нагрузка: {node.get("loadText", "Нет данных")}\nВходящий трафик: {node.get("rxBytesPerSec", "Нет данных")}\nИсходящий трафик: {node.get("txBytesPerSec", "Нет данных")}\nАктивные подключения: {online if isinstance(online,int) else "Нет данных"}\nПоследняя связь: {node.get("lastStatusChange") or "Нет данных"}\nПорог нагрузки: 75%; требуется внимание: 90%.\nПоказатели без данных не оцениваются. Статус связи сам по себе не подтверждает работу VPN на устройстве.'
        return Screen(escape(name+'\n\n'+body),[[('🔄 Обновить',f'admin:r:status:node:{parts[2]}')],[('← Назад','admin:r:home')]],rich=document_layout('🖥 '+name,body),structured=True)
    page = max(0,int(parts[1])) if len(parts)>1 and parts[1].isdigit() else 0
    start = page*5
    body = f'Проверено: {timestamp}\nПанелей: {len(panels)} · доступны: {sum(ok for _,_,ok in results)}\nVPN-серверов: {len(entries)}'
    for panel,nodes,ok in results:
        body += f'\n{"🟢" if ok else "🔴"} Панель {panel.name}: {"API доступен" if ok else "нет связи с API"}'
    body += '\nВыберите сервер для подробной информации.\n🟢 На связи / в норме · 🟡 нагрузка от 75% · 🔴 нет связи или нагрузка от 90%.\nШкалы показывают текущий снимок. Если панель не передаёт нагрузку, отображается «Нет данных».'
    rows = [[(f'{state(n)} · {n.get("name") or "Сервер"}',f'admin:r:status:node:{i}')] for i,(p,n) in enumerate(entries) if start<=i<start+5]
    nav=[]
    if page: nav.append(('←',f'admin:r:home:{page-1}'))
    if start+5<len(entries): nav.append(('→',f'admin:r:home:{page+1}'))
    if nav: rows.append(nav)
    rows += [[('🔄 Обновить',f'admin:r:home:{page}')],[('⚙️ Подключение','admin:r:connections')],[('← Назад','admin:home')]]
    return Screen(escape('📊 Статус серверов\n\n'+body),rows,rich=document_layout('📊 Статус серверов',body),structured=True)
