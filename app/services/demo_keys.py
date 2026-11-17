import secrets
import hashlib
from io import BytesIO
import qrcode
from html import escape
from datetime import timezone
from pathlib import PurePath
from app.database.models import now
from app.services.screens import Screen
from app.services.keys import KeyService


def demo_enabled(repo):
    """Only explicit test session factories on disposable DBs may issue demos.

    Database names, payment sandbox mode and environment variables alone never
    enable this. Production Database.sessions does not set this private flag.
    """
    if repo.session.info.get('isolated_demo_provisioning') is not True:
        return False
    bind=repo.session.bind
    url=getattr(bind,'engine',bind).url
    name=PurePath(url.database or '').name
    if bind.dialect.name=='sqlite':
        return name in (':memory:','test.db') or name.startswith('test_')
    return bind.dialect.name=='postgresql' and (name.startswith('test_') or name.endswith(('_test','_tests')))


async def create_demo_file(repo,key):
    if not demo_enabled(repo):
        raise ValueError('Тестовая выдача доступна только в изолированной тестовой среде.')
    text=f'F R E E M A N — ТЕСТОВЫЙ КЛЮЧ\n\nКлюч #{key.id}\nКод: DEMO-{secrets.token_hex(16)}\nДо: {key.expires_at:%d.%m.%Y %H:%M} UTC\n\nНЕРАБОЧИЙ ОБРАЗЕЦ. Не является конфигурацией подключения.\nИспользуется только для проверки выдачи, установки кнопок и подарка.\n'
    await repo.set_setting(f'demo_file:{key.id}',text)


async def key_screen(repo,settings,user,key_id,sid,format='qr'):
    if not demo_enabled(repo):
        raise ValueError('Рабочее VPN-подключение ещё не подтверждено. Обратитесь в поддержку.')
    if format not in ('config','link','qr'):
        raise ValueError('Формат недоступен.')
    sub=await repo.subscription(user.id,sid)
    if not sub or sub.expires_at.replace(tzinfo=timezone.utc)<=now() or await repo.setting(f'subscription_status:{sid}','active')!='active' or await repo.setting(f'subscription_key:{sid}')!=str(key_id):
        raise ValueError('Подписка недоступна.')
    key=await KeyService(repo,settings).owned(user.id,key_id)
    if not key or key.status!='active' or key.expires_at.replace(tzinfo=timezone.utc)<=now():
        raise ValueError('Ключ недоступен.')
    contents=await repo.setting(f'demo_file:{key.id}')
    if not contents:
        raise ValueError('Тестовый файл недоступен.')
    link=f'https://example.invalid/freeman-demo/{key.id}/{hashlib.sha256(contents.encode()).hexdigest()[:32]}'
    rows=[[('📲 Установить подписку',f'install:{sid}')],
          [('🔗 Скопировать ключ','copy:'+link)],
          [('📄 Скачать конфигурацию',f'demo_download:{key.id}:{sid}:config')],
          [('📖 Подробные инструкции',f'instructions:{sid}')],
          [('🎬 Видеоинструкция',f'video_instruction:{sid}')],
          [('📱 Моё устройство',f'devices:{sid}')],
          [('🔄 Продлить',f'renew:{sid}')],
          [('🎁 Подарить другу',f'g:input:subscription:{sid}')],
          [('⚙️ Дополнительно',f'subscription_extra:{sid}')],
          [('← Назад','subscriptions')]]
    from app.services.screens import time_remaining
    trial=f'\nПробный период: {round((sub.expires_at-sub.created_at).total_seconds()/3600)} ч.' if sub.kind=='trial' else ''
    name=await repo.setting(f'subscription_name:{sub.id}',key.name)
    text=f'🔑 <b>{escape(name)}</b>\nНазвание: {escape(name)}{trial}\nОсталось: {time_remaining(key.expires_at)}\nДо: {key.expires_at:%d.%m.%Y %H:%M} UTC\n\n<b>Как подключить</b>\n1. Установите AmneziaVPN или AmneziaWG.\n2. Отсканируйте QR-код либо скачайте конфигурацию.\n3. Импортируйте ключ и включите подключение.\n\nОдна подписка — один телефон. Не передавайте ссылку, QR-код или конфигурацию другим людям.\n\nНЕРАБОЧИЙ ОБРАЗЕЦ. Ссылка, QR-код и конфигурация предназначены для проверки выдачи, подключиться по ним нельзя.'
    if format=='link':
        return Screen(text+f'\n\nТестовая ссылка:\n<code>{escape(link)}</code>',rows)
    if format=='qr':
        stream=BytesIO()
        qrcode.make(link).save(stream,format='PNG')
        return Screen(text+'\n\nQR-код содержит ту же тестовую ссылку.',rows,photo=(stream.getvalue(),f'freeman-demo-{key.id}.png'))
    config='\n'.join('# '+line for line in contents.splitlines())+'\n\n[Interface]\nPrivateKey = DEMO-NOT-A-VALID-KEY\nAddress = 192.0.2.2/32\n\n[Peer]\nPublicKey = DEMO-NOT-A-VALID-KEY\nAllowedIPs = 0.0.0.0/0\nEndpoint = example.invalid:51820\n'
    return Screen(text+'\n\nТестовый файл конфигурации приложен.',rows,document=(config.encode('utf-8'),f'freeman-demo-{key.id}.conf'))
