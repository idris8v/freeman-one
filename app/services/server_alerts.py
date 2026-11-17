"""Notify on sustained resource incidents, escalation and recovery."""
import json
from hashlib import sha256
from shutil import disk_usage

from app.database.models import now
from app.database.repositories import SQLRepository
from app.services.activity import infrastructure_target, record
from app.services.remnawave import RemnawaveService
from app.services.server_status import read_panels


INTERVAL = 300
RECOVERY_SECONDS = 15 * 60
REMINDER_SECONDS = 60 * 60


def percent(value):
    return type(value) in (int, float) and 0 <= value <= 100


def node_sample(panel_id, node):
    identity = node.get('uuid') or node.get('id') or node.get('name')
    if not identity or node.get('isDisabled') or node.get('isConnected') is not True:
        return None
    metrics = {label: node[key] for key, label in (('cpuUsage', 'CPU'), ('memoryUsage', 'Память'))
               if percent(node.get(key))}
    system = node.get('system') or {}
    load = (system.get('stats') or {}).get('loadAvg') or []
    cores = (system.get('info') or {}).get('cpus')
    if 'CPU' not in metrics and load and type(load[0]) in (int,float) and load[0] >= 0 and type(cores) in (int,float) and cores > 0:
        metrics['Нагрузка на ядра'] = load[0] / cores * 100
    if not metrics:
        return None
    return f'vpn:{panel_id}:{identity}', 'VPN · ' + str(node.get('name') or 'Сервер')[:120], metrics


async def observe(repo, identity, name, metrics, high, low, sample=None, *, critical=None):
    timestamp = (sample or now()).timestamp()
    key = sha256(identity.encode()).hexdigest()[:24]
    setting = 'infra_alert_state:' + key
    saved = json.loads(await repo.setting(setting, '{}'))
    gap = timestamp - saved.get('sampled', 0)
    if gap < INTERVAL - 30:
        return

    recent = gap <= INTERVAL * 2
    active = bool(saved.get('active'))
    last_notice = saved.get('last_notice') or timestamp
    triggering = set(saved.get('triggering', []))
    escalated = bool(saved.get('escalated'))
    consecutive = saved.get('consecutive', 0) if recent else 0
    recovery_since = saved.get('recovery_since') if recent else None
    level = max(metrics.values())
    above = {label for label, value in metrics.items() if value >= high}
    title = ''

    if active:
        triggering.update(above)
        if triggering <= metrics.keys() and level <= low:
            if recovery_since is None:
                recovery_since = timestamp
                consecutive = 0
            consecutive += 1
        else:
            recovery_since = None
            consecutive = 0
        recovered = (
            consecutive >= 2 if critical is not None
            else consecutive >= 4 and recovery_since is not None and timestamp - recovery_since >= RECOVERY_SECONDS
        )
        if recovered:
            title = '✅ Место на диске освободилось' if critical is not None else '✅ Нагрузка в норме'
            active, escalated = False, False
            triggering.clear()
            consecutive, recovery_since = 0, None
        elif critical is not None and level >= critical and not escalated:
            title = '🚨 Диск почти заполнен'
            escalated = True
        elif critical is None and above and timestamp - last_notice >= REMINDER_SECONDS:
            title = '⚠️ Высокая нагрузка сохраняется'
    else:
        consecutive = consecutive + 1 if above else 0
        if critical is not None and level >= critical:
            title = '🚨 Диск почти заполнен'
            escalated = True
        elif consecutive >= (2 if critical is not None else 3):
            title = '⚠️ Диск заполняется' if critical is not None else '⚠️ Высокая нагрузка'
        if title:
            active, consecutive, triggering = True, 0, above

    if title:
        detail = ' · '.join(f'{label}: {value:.1f}%' for label, value in metrics.items())
        await record(repo, f'infra.alert:{key}:{int(timestamp)}', f'{title}\n{name}\n{detail}')
        last_notice = timestamp
    await repo.set_setting(setting, json.dumps(dict(active=active, consecutive=consecutive,
                                                   sampled=timestamp, triggering=sorted(triggering),
                                                   last_notice=last_notice, recovery_since=recovery_since,
                                                   escalated=escalated)))


async def observe_connection(repo, identity, name, online, *, panel=False, sample=None):
    timestamp = (sample or now()).timestamp()
    key = sha256(f'connection:{identity}'.encode()).hexdigest()[:24]
    setting = 'infra_alert_state:' + key
    saved = json.loads(await repo.setting(setting, '{}'))
    gap = timestamp - saved.get('sampled', 0)
    if gap < INTERVAL - 30:
        return
    active = bool(saved.get('active'))
    recent = gap <= INTERVAL + 120
    consecutive = saved.get('consecutive', 0) if recent else 0
    expected = active  # Offline confirms an outage, online confirms recovery.
    consecutive = consecutive + 1 if online is expected else 0
    threshold = 2 if active else 3
    if consecutive >= threshold:
        active = not active
        consecutive = 0
        if panel:
            title = '🔴 API Remnawave не отвечает' if active else '✅ API Remnawave снова доступен'
            detail = 'Статус узлов пока неизвестен.' if active else 'Проверка узлов возобновлена.'
        else:
            title = '🔴 Узел VPN потерял связь с Remnawave' if active else '✅ Узел VPN снова на связи с Remnawave'
            detail = 'Панель отметила узел как недоступный.' if active else 'Панель снова видит узел.'
        await record(repo, f'infra.alert:{key}:{int(timestamp)}', f'{title}\n{name}\n{detail}')
    await repo.set_setting(setting, json.dumps(dict(active=active, consecutive=consecutive, sampled=timestamp)))


async def prepare(database, settings):
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        if not await infrastructure_target(repo):
            return
        disk = disk_usage('/')
        await observe(repo, 'app-disk', 'Сервер сайта и бота', {'Диск': disk.used / disk.total * 100}, 85, 80, critical=95)
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        for panel, nodes, reachable in await read_panels(RemnawaveService(repo, settings), active_only=True):
            await observe_connection(repo, f'panel:{panel.id}', f'Панель · {panel.name}', reachable, panel=True)
            if not reachable:
                continue
            for node in nodes:
                identity = node.get('uuid') or node.get('id') or node.get('name')
                if not identity or node.get('isDisabled') is True:
                    continue
                name = 'VPN · ' + str(node.get('name') or 'Сервер')[:120]
                country = node.get('countryCode')
                if isinstance(country, str) and len(country) == 2 and country.isascii() and country.isalpha():
                    name += f' · {country.upper()}'
                await observe_connection(repo, f'vpn:{panel.id}:{identity}', name, node.get('isConnected'))
                sample = node_sample(panel.id, node)
                if sample:
                    await observe(repo, *sample, 90, 80)
