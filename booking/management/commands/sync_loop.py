import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from booking.avito import push_avito_changes, sync_avito
from booking.ical import sync_all

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Фоновая синхронизация: календари площадок (iCal) и брони Авито (API) — каждые SYNC_INTERVAL_SECONDS секунд; "
        "изменения занятости и цен уходят на Авито в течение AVITO_PUSH_CHECK_SECONDS секунд"
    )

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=settings.SYNC_INTERVAL_SECONDS)

    def handle(self, *args, interval, **options):
        stop = {"flag": False}

        def _stop(*_):
            stop["flag"] = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        push_every = max(5, settings.AVITO_PUSH_CHECK_SECONDS)
        log.info("Синхронизация запущена, интервал %s с, проверка изменений для Авито — каждые %s с", interval, push_every)
        next_full = next_push = 0.0
        while not stop["flag"]:
            now = time.monotonic()
            if now >= next_full:
                next_full = now + interval
                next_push = now + push_every
                self._run(self._full_sync)
            elif now >= next_push:
                next_push = now + push_every
                self._run(push_avito_changes)
            time.sleep(1)
        log.info("Синхронизация остановлена")

    @staticmethod
    def _run(func):
        close_old_connections()
        try:
            func()
        except Exception:  # noqa: BLE001 — цикл не должен умирать
            log.exception("Сбой цикла синхронизации")

    @staticmethod
    def _full_sync():
        for name, r in sync_all().items():
            log.info("%s: ok=%s +%s ~%s -%s", name, r.ok, len(r.created), len(r.updated), len(r.cancelled))
        result = sync_avito()
        if result is not None:
            log.info("API Авито: ok=%s событий %s %s", result.ok, len(result.events), result.error)
