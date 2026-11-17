"""Host-level health monitor with Telegram alerts on state changes."""
import json
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

ROOT=Path('/opt/freemanone')
STATE=Path('/var/lib/freeman-monitor-state.json')


def environment():
    result={}
    for line in (ROOT/'.env').read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key,value=line.split('=',1)
        result[key.strip()]=value.strip().strip('"').strip("'")
    return result


def check():
    problems=[]
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=10) as response:
            body=json.loads(response.read())
        if response.status!=200 or body.get('status')!='ok':
            problems.append('веб-сайт или база данных не готовы')
        elif body.get('bot') not in ('polling','standby'):
            problems.append('веб-сайт доступен; Telegram-бот: '+str(body.get('bot','unknown')))
    except Exception:
        problems.append('health-check бота недоступен')
    try:
        result=subprocess.run(['/usr/bin/docker','compose','ps','--status','running','--services'],cwd=ROOT,text=True,capture_output=True,timeout=20,check=False)
        if result.returncode or 'bot' not in result.stdout.split():
            problems.append('контейнер bot не запущен')
    except Exception:
        problems.append('не удалось проверить Docker')
    free=shutil.disk_usage(ROOT).free/shutil.disk_usage(ROOT).total*100
    if free<10:
        problems.append(f'на диске осталось {free:.1f}%')
    try:
        timer=subprocess.run(['/usr/bin/systemctl','is-active','freemanone-backup.timer'],text=True,capture_output=True,timeout=10,check=False)
        if timer.stdout.strip()!='active':
            problems.append('таймер локальных копий не активен')
    except Exception:
        problems.append('не удалось проверить таймер копий')
    return problems


def notify(env,text):
    token=env.get('BOT_TOKEN','')
    ids=[item.strip() for item in env.get('ADMIN_IDS','').split(',') if item.strip().isdigit()]
    for telegram_id in ids:
        try:
            data=urllib.parse.urlencode({'chat_id':telegram_id,'text':text}).encode()
            request=urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage',data=data)
            urllib.request.urlopen(request,timeout=15).read()
        except Exception:
            pass


def main():
    env=environment()
    problems=check()
    current='failed' if problems else 'healthy'
    try:
        previous=json.loads(STATE.read_text()).get('state')
    except Exception:
        previous=None
    if current=='failed' and previous!='failed':
        notify(env,'🚨 F R E E M A N: обнаружен сбой сервера\n\n'+'\n'.join('• '+item for item in problems))
    elif current=='healthy' and previous=='failed':
        notify(env,'✅ F R E E M A N: сервер восстановлен, бот и база данных отвечают.')
    STATE.parent.mkdir(parents=True,exist_ok=True)
    STATE.write_text(json.dumps({'state':current,'problems':problems},ensure_ascii=False),encoding='utf-8')


if __name__=='__main__':
    main()
