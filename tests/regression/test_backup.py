import sqlite3
from pathlib import Path
import importlib.util
import pytest
from app.utils.backup import backup


def test_backup_is_readable_and_preserves_data(tmp_path, monkeypatch):
    database = tmp_path / 'database.db'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE sample (value TEXT)')
        connection.execute("INSERT INTO sample VALUES ('persistent')")
    monkeypatch.setenv('DATABASE_URL', 'sqlite+aiosqlite:///' + database.as_posix())
    backup()
    snapshots = list((tmp_path / 'backups').glob('freeman-*.db'))
    assert len(snapshots) == 1
    with sqlite3.connect(snapshots[0]) as connection:
        assert connection.execute('SELECT value FROM sample').fetchone()[0] == 'persistent'


def test_token_config_preserves_settings_and_rejects_invalid_input(tmp_path):
    script = Path(__file__).resolve().parents[2] / 'scripts/configure_token.py'
    spec = importlib.util.spec_from_file_location('configure_token', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = tmp_path / '.env'
    env.write_text('BOT_TOKEN=\nADMIN_IDS=8066410491\nPRICE_1=400\n', encoding='utf-8')
    token = '123456:' + 'A' * 30  # mock token, never sent to Telegram
    module.save_token(env, token)
    text = env.read_text(encoding='utf-8')
    assert text.count('BOT_TOKEN=') == 1
    assert 'ADMIN_IDS=8066410491' in text
    assert 'PRICE_1=400' in text
    with pytest.raises(ValueError):
        module.save_token(env, 'bad\nADMIN_IDS=999')
    assert env.read_text(encoding='utf-8') == text


def test_bot_identity_config_is_atomic_and_validated(tmp_path):
    script = Path(__file__).resolve().parents[2] / 'scripts/configure_token.py'
    spec = importlib.util.spec_from_file_location('configure_identity', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = tmp_path / '.env'
    env.write_text('BOT_TOKEN=old\nBOT_USERNAME=OldBot\nADMIN_IDS=8066410491\n', encoding='utf-8')
    token = '123456:' + 'A' * 30
    module.save_identity(env, token, '@NewFreemanBot')
    text = env.read_text(encoding='utf-8')
    assert text.count('BOT_TOKEN=') == text.count('BOT_USERNAME=') == 1
    assert 'BOT_USERNAME=NewFreemanBot' in text and 'ADMIN_IDS=8066410491' in text
    for username in ('bad name', 'short', 'not_a_service'):
        with pytest.raises(ValueError):
            module.save_identity(env, token, username)
    assert env.read_text(encoding='utf-8') == text


def test_identity_verification_checks_telegram_username(monkeypatch):
    script = Path(__file__).resolve().parents[2] / 'scripts/configure_token.py'
    spec = importlib.util.spec_from_file_location('verify_identity', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): return b'{"ok":true,"result":{"username":"FreemanOneBot"}}'
    monkeypatch.setattr(module,'urlopen',lambda *args,**kwargs: Response())
    module.verify_identity('123456:'+'A'*30,'FreemanOneBot')
    with pytest.raises(ValueError):
        module.verify_identity('123456:'+'A'*30,'another_bot')
