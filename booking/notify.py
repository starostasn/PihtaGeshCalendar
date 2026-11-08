"""Уведомления владельцу в Telegram. Если бот не настроен — молча ничего не делаем."""

import logging

import requests
from django.conf import settings

log = logging.getLogger(__name__)


def telegram_configured() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)


def send_telegram(text: str) -> bool:
    if not telegram_configured():
        log.info("Telegram не настроен, сообщение не отправлено: %s", text)
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:  # уведомление не должно ронять синхронизацию
        log.warning("Не удалось отправить сообщение в Telegram: %s", exc)
        return False
