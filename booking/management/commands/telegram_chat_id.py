import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Показать chat_id: сначала напишите своему боту любое сообщение, затем запустите команду"

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("Сначала укажите TELEGRAM_BOT_TOKEN в .env")
        resp = requests.get(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getUpdates", timeout=15
        )
        resp.raise_for_status()
        chats = {}
        for upd in resp.json().get("result", []):
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat")
            if chat:
                chats[chat["id"]] = chat.get("username") or chat.get("first_name") or ""
        if not chats:
            self.stdout.write("Сообщений не найдено. Напишите боту /start и повторите.")
        for chat_id, name in chats.items():
            self.stdout.write(f"TELEGRAM_CHAT_ID={chat_id}   ({name})")
