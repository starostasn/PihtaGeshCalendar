from django.core.management.base import BaseCommand

from booking.ical import sync_all


class Command(BaseCommand):
    help = "Один раз забрать iCal-календари всех площадок"

    def handle(self, *args, **options):
        results = sync_all()
        if not results:
            self.stdout.write("Нет площадок со ссылкой на календарь.")
        for name, r in results.items():
            status = "OK" if r.ok else f"ОШИБКА: {r.error}"
            self.stdout.write(
                f"{name}: {status}; новых {len(r.created)}, изменено {len(r.updated)}, отменено {len(r.cancelled)}"
            )
