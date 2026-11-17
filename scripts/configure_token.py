"""Enter a bot identity on the server without exposing its token."""
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.request import Request, urlopen


def save_token(path, token):
    token = token.strip()
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', token):
        raise ValueError('Неверный формат токена. Скопируйте токен из BotFather целиком.')
    lines = path.read_text(encoding='utf-8').splitlines()
    lines = [line for line in lines if not line.startswith('BOT_TOKEN=')]
    lines.insert(0, 'BOT_TOKEN=' + token)
    temporary = path.with_name('.env.new')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
        file.write('\n'.join(lines) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def save_identity(path, token, username):
    token = token.strip()
    username = username.strip().lstrip('@')
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', token):
        raise ValueError('Неверный формат токена. Скопируйте токен из BotFather целиком.')
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username) or not username.lower().endswith('bot'):
        raise ValueError('Неверный username: нужно 5–32 символа, окончание bot, без @.')
    lines = path.read_text(encoding='utf-8').splitlines()
    lines = [line for line in lines if not line.startswith(('BOT_TOKEN=', 'BOT_USERNAME='))]
    lines[0:0] = ['BOT_TOKEN=' + token, 'BOT_USERNAME=' + username]
    temporary = path.with_name('.env.new')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
        file.write('\n'.join(lines) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def verify_identity(token, username):
    request = Request(f'https://api.telegram.org/bot{token}/getMe', method='POST')
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except Exception as error:
        raise ValueError('Не удалось проверить токен через Telegram. Настройки не изменены.') from error
    actual = str(payload.get('result', {}).get('username', ''))
    if not payload.get('ok') or actual.lower() != username.lower():
        raise ValueError(f'Токен принадлежит @{actual or "неизвестному боту"}, ожидался @{username}. Настройки не изменены.')


if __name__ == '__main__':
    path = Path(__file__).resolve().parent.parent / '.env'
    try:
        username = sys.argv[1] if len(sys.argv) == 2 else input('Новый username бота без @: ')
        username = username.strip().lstrip('@')
        token = getpass.getpass('Вставьте токен BotFather и нажмите Enter (ввод скрыт): ')
        verify_identity(token, username)
        backup = path.with_name('.env.identity-backup')
        shutil.copy2(path, backup)
        backup.chmod(0o600)
        save_identity(path, token, username)
    except (ValueError, KeyboardInterrupt) as error:
        print(str(error) or 'Отменено.')
        raise SystemExit(1)
    print('Username и токен сохранены. Сообщите в чате, что готово.')
