"""Configure YooKassa credentials interactively without shell history exposure."""
import base64
import getpass
import json
import os
from pathlib import Path
import re
import shutil
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def verify(shop_id, secret_key):
    auth = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()
    request = Request(
        "https://api.yookassa.ru/v3/payments?limit=1",
        headers={"Authorization": "Basic " + auth, "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except HTTPError as error:
        if error.code in (401, 403):
            raise ValueError("ЮKassa отклонила Shop ID или секретный ключ.") from error
        raise ValueError(f"ЮKassa вернула HTTP {error.code}.") from error
    except (URLError, TimeoutError) as error:
        raise ValueError("Не удалось связаться с API ЮKassa. Настройки не изменены.") from error
    if not isinstance(payload, dict) or "items" not in payload:
        raise ValueError("ЮKassa вернула неожиданный ответ. Настройки не изменены.")


def save(path, shop_id, secret_key, test_mode):
    lines = path.read_text(encoding="utf-8").splitlines()
    prefixes = ("YOOKASSA_SHOP_ID=", "YOOKASSA_SECRET_KEY=", "YOOKASSA_TEST_MODE=")
    lines = [line for line in lines if not line.startswith(prefixes)]
    lines.extend([
        "YOOKASSA_SHOP_ID=" + shop_id,
        "YOOKASSA_SECRET_KEY=" + secret_key,
        "YOOKASSA_TEST_MODE=" + ("true" if test_mode else "false"),
    ])
    temporary = path.with_name(".env.yookassa-new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")
    temporary.replace(path)
    path.chmod(0o600)


if __name__ == "__main__":
    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        shop = input("Shop ID ЮKassa: ").strip()
        secret = getpass.getpass("Секретный ключ (ввод скрыт): ").strip()
        mode = input("Режим TEST? [да/нет]: ").strip().lower()
        if not re.fullmatch(r"\d{3,32}", shop):
            raise ValueError("Shop ID должен состоять из цифр.")
        if len(secret) < 16 or any(character.isspace() for character in secret):
            raise ValueError("Секретный ключ имеет неверный формат.")
        if mode not in ("да", "д", "yes", "y", "нет", "н", "no", "n"):
            raise ValueError("Ответьте да или нет.")
        verify(shop, secret)
        backup = env_path.with_name(".env.yookassa-backup")
        shutil.copy2(env_path, backup)
        backup.chmod(0o600)
        save(env_path, shop, secret, mode in ("да", "д", "yes", "y"))
    except (ValueError, KeyboardInterrupt) as error:
        print(str(error) or "Отменено.")
        raise SystemExit(1)
    print("ЮKassa проверена и сохранена. Выполните: docker compose up -d --force-recreate bot")
