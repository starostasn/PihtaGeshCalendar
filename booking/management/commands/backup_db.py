import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Резервная копия базы (безопасно при работающем сервисе). Хранит последние N копий."

    def add_arguments(self, parser):
        parser.add_argument("--keep", type=int, default=30)

    def handle(self, *args, keep, **options):
        db_path = Path(settings.DATABASES["default"]["NAME"])
        backup_dir = settings.DATA_DIR / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"db-{datetime.now():%Y%m%d-%H%M%S}.sqlite3"
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(target)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        backups = sorted(backup_dir.glob("db-*.sqlite3"))
        for old in backups[:-keep]:
            old.unlink()
        self.stdout.write(f"Копия сохранена: {target}")
