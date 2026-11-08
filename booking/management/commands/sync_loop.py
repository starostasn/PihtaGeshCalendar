import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from booking.ical import sync_all

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Фоновая синхронизация календарей площадок каждые SYNC_INTERVAL_SECONDS секунд"

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=settings.SYNC_INTERVAL_SECONDS)

    def handle(self, *args, interval, **options):
        stop = {"flag": False}

        def _stop(*_):
            stop["flag"] = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        log.info("Синхронизация запущена, интервал %s с", interval)
        while not stop["flag"]:
            close_old_connections()
            try:
                for name, r in sync_all().items():
                    log.info("%s: ok=%s +%s ~%s -%s", name, r.ok, len(r.created), len(r.updated), len(r.cancelled))
            except Exception:  # noqa: BLE001 — цикл не должен умирать
                log.exception("Сбой цикла синхронизации")
            for _ in range(interval):
                if stop["flag"]:
                    break
                time.sleep(1)
        log.info("Синхронизация остановлена")
