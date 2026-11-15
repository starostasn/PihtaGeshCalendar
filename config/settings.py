"""Настройки проекта. Всё, что зависит от окружения, берётся из переменных окружения (.env)."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-key-change-me")
DEBUG = env_bool("DEBUG", default=True)
if not DEBUG and SECRET_KEY == "dev-insecure-key-change-me":
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("Задайте SECRET_KEY в .env")

DOMAIN = os.environ.get("DOMAIN", "")
ALLOWED_HOSTS = [h for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
if DOMAIN:
    ALLOWED_HOSTS.append(DOMAIN)
    CSRF_TRUSTED_ORIGINS = [f"https://{DOMAIN}"]

# Абсолютный адрес сервиса — нужен, чтобы показывать полные ссылки на iCal-календари.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", f"https://{DOMAIN}" if DOMAIN else "http://localhost:8000")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "booking",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "booking.middleware.SecurityHeadersMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "booking.context_processors.house",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# SQLite: для одного дома и одного пользователя этого более чем достаточно.
# IMMEDIATE-транзакции сериализуют запись, поэтому проверка пересечений броней
# и сохранение брони выполняются атомарно даже при параллельной работе веба и синхронизации.
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
    }
}

# Счётчики неудачных входов (защита от перебора паролей). Файловый кэш общий для всех процессов.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": DATA_DIR / "cache",
        "TIMEOUT": 15 * 60,
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = os.environ.get("TIME_ZONE", "Europe/Moscow")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "calendar"
LOGOUT_REDIRECT_URL = "login"

# На сервере сервис работает за HTTPS (Caddy). При запуске на своём компьютере (run_local.py)
# адрес http://127.0.0.1, поэтому там USE_HTTPS=0.
USE_HTTPS = env_bool("USE_HTTPS", default=not DEBUG)
if USE_HTTPS:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # HSTS: браузер год будет открывать сайт только по HTTPS (без поддоменов — они могут жить отдельно)
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", str(365 * 24 * 3600)))

# Заголовки безопасности (см. booking/middleware.py). Скрипты — только свои файлы, никаких встроенных
# и чужих; встраивать календарь в чужие сайты нельзя; формы отправляются только сюда же.
CONTENT_SECURITY_POLICY = os.environ.get(
    "CONTENT_SECURITY_POLICY",
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'",
)
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Служебная админка Django (/admin/) по умолчанию выключена: всё нужное есть в самом календаре,
# а лишняя страница входа — лишняя цель для подбора паролей.
ENABLE_DJANGO_ADMIN = env_bool("ENABLE_DJANGO_ADMIN", default=False)

SILENCED_SYSTEM_CHECKS = [
    "security.W008",  # HTTP -> HTTPS перенаправляет Caddy; в Django это сломало бы проверку здоровья контейнера
    "security.W005",  # HSTS без поддоменов — намеренно, поддомены могут обслуживаться отдельно
    "security.W021",  # в список предзагрузки браузеров не вносим — это необратимо на месяцы
]
SESSION_COOKIE_AGE = 60 * 60 * 24 * 30  # месяц, чтобы не логиниться с телефона каждый день

# Telegram-уведомления владельцу (необязательно). Как получить — см. README.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Синхронизация календарей площадок
SYNC_INTERVAL_SECONDS = int(os.environ.get("SYNC_INTERVAL_SECONDS", "600"))
SYNC_HTTP_TIMEOUT = int(os.environ.get("SYNC_HTTP_TIMEOUT", "20"))

# API Авито: ключи приложения с developers.avito.ru. Хранятся только в .env сервера.
AVITO_CLIENT_ID = os.environ.get("AVITO_CLIENT_ID", "").strip()
AVITO_CLIENT_SECRET = os.environ.get("AVITO_CLIENT_SECRET", "").strip()
# Как часто проверять, не изменилась ли занятость или цены, чтобы сразу отправить их на Авито
AVITO_PUSH_CHECK_SECONDS = int(os.environ.get("AVITO_PUSH_CHECK_SECONDS", "30"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
