from django.core.management.base import BaseCommand

from booking.avito import sync_avito
from booking.ical import sync_all


class Command(BaseCommand):
    help = "Один раз забрать iCal-календари всех площадок и брони Авито (API)"

    def handle(self, *args, **options):
        results = sync_all()
        if not results:
            self.stdout.write("Нет площадок со ссылкой на календарь.")
        for name, r in results.items():
            status = "OK" if r.ok else f"ОШИБКА: {r.error}"
            self.stdout.write(
                f"{name}: {status}; новых {len(r.created)}, изменено {len(r.updated)}, отменено {len(r.cancelled)}"
            )
        avito = sync_avito()
        if avito is None:
            self.stdout.write("API Авито: не подключено.")
        else:
            self.stdout.write("API Авито: " + ("OK" if avito.ok else f"ОШИБКА: {avito.error}") + f"; событий {len(avito.events)}")
