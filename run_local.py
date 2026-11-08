"""Запуск сервиса на своём компьютере (Windows, macOS, Linux) без Docker и без сервера.

    python run_local.py

При первом запуске создаёт базу и спрашивает логин и пароль, затем запускает
веб-сервер на http://127.0.0.1:8000, фоновую синхронизацию площадок и открывает браузер.
Данные хранятся в папке data (переменная DATA_DIR). Остановка — закрыть окно или Ctrl+C.

Ограничение: площадки (Авито и др.) не смогут забирать наш календарь с домашнего
компьютера — работает только загрузка броней С площадок. Для полной синхронизации нужен сервер.
"""

import getpass
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data")).resolve()

SETTINGS_TEMPLATE = """\
# Настройки календаря. Строки с # — комментарии. После изменения перезапустите программу.

# Часовой пояс дома
TIME_ZONE=Europe/Moscow

# Уведомления в Telegram (необязательно). Токен бота — у @BotFather, chat_id — см. README.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Как часто забирать календари площадок, секунд
SYNC_INTERVAL_SECONDS=600

# Порт веб-сервера
PORT=8000
"""


def load_settings_file(path: Path) -> None:
    if not path.exists():
        path.write_text(SETTINGS_TEMPLATE, encoding="utf-8")
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip():
            os.environ.setdefault(key.strip(), value.strip())


def free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"Не нашёл свободный порт в диапазоне {preferred}–{preferred + 19}")


def prepare_environment() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    load_settings_file(DATA_DIR / "settings.txt")

    key_file = DATA_DIR / "secret_key.txt"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(50), encoding="utf-8")

    port = free_port(int(os.environ.get("PORT", "8000")))
    os.environ["DATA_DIR"] = str(DATA_DIR)
    os.environ.setdefault("SECRET_KEY", key_file.read_text(encoding="utf-8").strip())
    os.environ.setdefault("DEBUG", "0")
    os.environ.setdefault("USE_HTTPS", "0")
    os.environ.setdefault("PUBLIC_BASE_URL", f"http://127.0.0.1:{port}")
    os.environ.setdefault("ALLOWED_HOSTS", "127.0.0.1,localhost")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    return port


def ensure_user() -> None:
    from django.contrib.auth.models import User
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    if User.objects.exists():
        return
    print("\nПервый запуск. Придумайте логин и пароль для входа в календарь.")
    while True:
        username = input("Логин: ").strip()
        if username:
            break
    while True:
        password = getpass.getpass("Пароль (не меньше 10 символов, при вводе не отображается): ")
        try:
            validate_password(password)
        except ValidationError as exc:
            print("  " + " ".join(exc.messages))
            continue
        if getpass.getpass("Повторите пароль: ") != password:
            print("  Пароли не совпадают.")
            continue
        break
    User.objects.create_superuser(username=username, email="", password=password)
    print("Пользователь создан.\n")


def sync_forever(interval: int) -> None:
    from django.db import close_old_connections

    from booking.ical import sync_all

    while True:
        close_old_connections()
        try:
            sync_all()
        except Exception as exc:  # noqa: BLE001 — фоновый поток не должен умирать
            print(f"Ошибка синхронизации: {exc}")
        time.sleep(interval)


def main() -> None:
    port = prepare_environment()

    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command

    print("Подготовка базы данных…")
    call_command("migrate", interactive=False, verbosity=0)
    if not (Path(settings.STATIC_ROOT) / "staticfiles.json").exists():
        call_command("collectstatic", interactive=False, verbosity=0)
    ensure_user()

    threading.Thread(target=sync_forever, args=(settings.SYNC_INTERVAL_SECONDS,), daemon=True).start()

    from waitress import serve

    from config.wsgi import application

    url = f"http://127.0.0.1:{port}/"
    print("=" * 60)
    print(f"  Календарь работает: {url}")
    print(f"  Данные: {DATA_DIR}")
    print("  Чтобы остановить — закройте это окно.")
    print("=" * 60)
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    serve(application, host="127.0.0.1", port=port, threads=8, _quiet=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено.")
