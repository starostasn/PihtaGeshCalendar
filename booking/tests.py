from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .ical import FeedEvent, apply_feed, build_export, parse_feed, sync_channel
from .models import Booking, Channel, House, PriceRule
from .pricing import PriceCalculator
from .services import OverlapError, conflict_pairs, save_booking


def d(offset: int) -> date:
    return timezone.localdate() + timedelta(days=offset)


def next_weekday(weekday: int, after: int = 1) -> date:
    x = d(after)
    while x.weekday() != weekday:
        x += timedelta(days=1)
    return x


def ics(*events: str) -> bytes:
    body = "\r\n".join(events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//RU\r\n{body}\r\nEND:VCALENDAR\r\n".encode()


def vevent(uid: str | None, start: date, end: date | None, summary: str = "Reserved", extra: str = "") -> str:
    lines = ["BEGIN:VEVENT"]
    if uid is not None:
        lines.append(f"UID:{uid}")
    lines.append(f"DTSTART;VALUE=DATE:{start:%Y%m%d}")
    if end is not None:
        lines.append(f"DTEND;VALUE=DATE:{end:%Y%m%d}")
    lines.append(f"SUMMARY:{summary}")
    if extra:
        lines.append(extra)
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


class PricingTests(TestCase):
    def setUp(self):
        self.house = House.get()
        self.house.base_price = 10000
        self.house.min_stay = 2
        self.house.save()

    def test_base_price_and_min_stay(self):
        calc = PriceCalculator()
        q = calc.quote(d(10), d(13))
        self.assertEqual(q.nights_count, 3)
        self.assertEqual(q.total, 30000)
        self.assertEqual(q.min_stay, 2)
        self.assertTrue(q.min_stay_ok)
        self.assertFalse(calc.quote(d(10), d(11)).min_stay_ok)

    def test_rule_priority(self):
        friday = next_weekday(4)
        PriceRule.objects.create(name="Выходные", weekdays="4,5", price=14000, priority=PriceRule.PRIORITY_WEEKDAYS)
        PriceRule.objects.create(
            name="Сезон", date_from=friday - timedelta(days=10), date_to=friday + timedelta(days=10), price=12000
        )
        calc = PriceCalculator()
        # Сезон (приоритет 20) перекрывает выходные (10) — внутри сезона пятница стоит 12000.
        self.assertEqual(calc.night_price(friday).price, 12000)
        # Вне сезона пятница — по правилу выходных, четверг — базовая.
        later_friday = friday + timedelta(days=28)
        self.assertEqual(calc.night_price(later_friday).price, 14000)
        self.assertEqual(calc.night_price(later_friday - timedelta(days=1)).price, 10000)

        PriceRule.objects.create(
            name="Праздник", date_from=friday, date_to=friday, price=20000, min_stay=3,
            priority=PriceRule.PRIORITY_DATES,
        )
        calc = PriceCalculator()
        self.assertEqual(calc.night_price(friday).price, 20000)
        self.assertEqual(calc.min_stay(friday), 3)
        self.assertEqual(calc.min_stay(friday + timedelta(days=1)), 2)

    def test_inactive_rule_ignored(self):
        PriceRule.objects.create(name="Выкл", price=1, is_active=False)
        self.assertEqual(PriceCalculator().night_price(d(5)).price, 10000)

    def test_min_stay_only_rule_does_not_change_price(self):
        PriceRule.objects.create(name="Мин. 3", date_from=d(0), date_to=d(30), min_stay=3)
        calc = PriceCalculator()
        self.assertEqual(calc.night_price(d(5)).price, 10000)
        self.assertEqual(calc.min_stay(d(5)), 3)


class OverlapTests(TestCase):
    def test_adjacent_allowed_overlap_rejected(self):
        save_booking(Booking(check_in=d(10), check_out=d(13), guest_name="A"))
        save_booking(Booking(check_in=d(13), check_out=d(15), guest_name="B"))  # заезд в день выезда
        save_booking(Booking(check_in=d(8), check_out=d(10), guest_name="C"))
        with self.assertRaises(OverlapError):
            save_booking(Booking(check_in=d(12), check_out=d(14), guest_name="D"))
        with self.assertRaises(OverlapError):
            save_booking(Booking(kind=Booking.Kind.BLOCK, check_in=d(9), check_out=d(16)))

    def test_cancelled_does_not_block_and_edit_self_ok(self):
        a = save_booking(Booking(check_in=d(10), check_out=d(13)))
        a.check_out = d(14)
        save_booking(a)  # пересечение с самой собой — не конфликт
        a.status = Booking.Status.CANCELLED
        save_booking(a)
        save_booking(Booking(check_in=d(11), check_out=d(12)))


class ExportTests(TestCase):
    def test_export_excludes_own_imported_bookings_and_hides_guest(self):
        avito = Channel.objects.create(name="Авито")
        sutochno = Channel.objects.create(name="Суточно")
        Booking.objects.create(check_in=d(1), check_out=d(3), guest_name="Иван Петров", phone="+7900")
        Booking.objects.create(check_in=d(5), check_out=d(7), channel=avito, external_uid="av-1")
        Booking.objects.create(check_in=d(9), check_out=d(10), channel=avito)  # вручную, источник Авито
        Booking.objects.create(check_in=d(20), check_out=d(22), status=Booking.Status.CANCELLED)

        for_avito = parse_feed(build_export(avito))
        self.assertEqual({(e.start, e.end) for e in for_avito}, {(d(1), d(3)), (d(9), d(10))})

        for_sutochno = parse_feed(build_export(sutochno))
        self.assertEqual(len(for_sutochno), 3)
        raw = build_export(sutochno).decode()
        self.assertNotIn("Иван", raw)
        self.assertNotIn("+7900", raw)
        self.assertIn(f"DTEND;VALUE=DATE:{d(3):%Y%m%d}", raw)


class ParseFeedTests(TestCase):
    def test_variants(self):
        start = d(10)
        data = ics(
            vevent("a", start, start + timedelta(days=2)),
            vevent("b", start + timedelta(days=5), None),  # без DTEND — одна ночь
            vevent("c", start + timedelta(days=8), start + timedelta(days=9), extra="STATUS:CANCELLED"),
            vevent(None, start + timedelta(days=12), start + timedelta(days=14)),
            vevent("dup", start + timedelta(days=20), start + timedelta(days=21)),
            vevent("dup", start + timedelta(days=25), start + timedelta(days=26)),
            "BEGIN:VEVENT\r\nUID:dt\r\nDTSTART:%sT110000Z\r\nDTEND:%sT090000Z\r\nEND:VEVENT"
            % ((start + timedelta(days=30)).strftime("%Y%m%d"), (start + timedelta(days=32)).strftime("%Y%m%d")),
        )
        events = {e.uid: e for e in parse_feed(data)}
        self.assertEqual(events["a"].end, start + timedelta(days=2))
        self.assertEqual(events["b"].end, start + timedelta(days=6))
        self.assertNotIn("c", events)
        self.assertTrue(any(uid.startswith("nouid-") for uid in events))
        self.assertIn("dup", events)
        self.assertIn("dup#1", events)
        self.assertEqual((events["dt"].start, events["dt"].end), (start + timedelta(days=30), start + timedelta(days=32)))


class ApplyFeedTests(TestCase):
    def setUp(self):
        self.ch = Channel.objects.create(name="Авито", import_url="https://example.com/a.ics")

    def ev(self, uid, a, b):
        return FeedEvent(uid=uid, start=d(a), end=d(b), summary="Reserved")

    def active(self):
        return Booking.objects.active().filter(channel=self.ch)

    def test_create_update_cancel(self):
        r = apply_feed(self.ch, [self.ev("1", 5, 7), self.ev("2", 10, 12)])
        self.assertEqual(len(r.created), 2)
        r = apply_feed(self.ch, [self.ev("1", 5, 8), self.ev("2", 10, 12)])
        self.assertEqual(len(r.updated), 1)
        self.assertEqual(self.active().get(external_uid="1").check_out, d(8))
        r = apply_feed(self.ch, [self.ev("1", 5, 8)])
        self.assertEqual(len(r.cancelled), 1)
        self.assertEqual(Booking.objects.get(external_uid="2").status, Booking.Status.CANCELLED)
        # Бронь вернулась на площадке — восстанавливаем
        r = apply_feed(self.ch, [self.ev("1", 5, 8), self.ev("2", 10, 12)])
        self.assertEqual(len(r.updated), 1)
        self.assertEqual(self.active().count(), 2)

    def test_past_bookings_untouched(self):
        Booking.objects.create(channel=self.ch, external_uid="old", check_in=d(-10), check_out=d(-7))
        apply_feed(self.ch, [self.ev("1", 5, 7)])
        self.assertEqual(Booking.objects.get(external_uid="old").status, Booking.Status.ACTIVE)

    def test_empty_feed_does_not_mass_cancel(self):
        apply_feed(self.ch, [self.ev("1", 5, 7), self.ev("2", 10, 12)])
        r = apply_feed(self.ch, [])
        self.assertEqual(r.cancelled, [])
        self.assertTrue(r.warnings)
        self.assertEqual(self.active().count(), 2)

    def test_empty_feed_cancels_single_booking(self):
        apply_feed(self.ch, [self.ev("1", 5, 7)])
        r = apply_feed(self.ch, [])
        self.assertEqual(len(r.cancelled), 1)

    def test_uid_change_matched_by_dates(self):
        apply_feed(self.ch, [self.ev("old-uid", 5, 7)])
        r = apply_feed(self.ch, [self.ev("new-uid", 5, 7)])
        self.assertEqual((r.created, r.updated, r.cancelled), ([], [], []))
        self.assertEqual(self.active().get().external_uid, "new-uid")


class SyncChannelTests(TestCase):
    def setUp(self):
        self.ch = Channel.objects.create(name="Авито", import_url="https://example.com/a.ics")
        self.sent = []

    def notify(self, text):
        self.sent.append(text)
        return True

    def test_new_booking_and_conflict_notification(self):
        save_booking(Booking(check_in=d(10), check_out=d(12), guest_name="Прямой"))
        data = ics(vevent("x", d(11), d(13)))
        r = sync_channel(self.ch, fetch=lambda url: data, notify=self.notify)
        self.assertTrue(r.ok)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Новая бронь", self.sent[0])
        self.assertIn("ДВОЙНАЯ БРОНЬ", self.sent[0])
        self.assertEqual(len(conflict_pairs()), 1)
        self.ch.refresh_from_db()
        self.assertTrue(self.ch.last_sync_ok)
        self.assertEqual(self.ch.sync_logs.count(), 1)

        # Повторная синхронизация без изменений — без уведомлений
        sync_channel(self.ch, fetch=lambda url: data, notify=self.notify)
        self.assertEqual(len(self.sent), 1)

    def test_echo_hint(self):
        save_booking(Booking(check_in=d(10), check_out=d(12)))
        sync_channel(self.ch, fetch=lambda url: ics(vevent("x", d(10), d(12))), notify=self.notify)
        self.assertIn("вашу же бронь", self.sent[0])

    def test_failure_notified_once_then_recovery(self):
        def boom(url):
            raise ConnectionError("timeout")

        r = sync_channel(self.ch, fetch=boom, notify=self.notify)
        self.assertFalse(r.ok)
        sync_channel(self.ch, fetch=boom, notify=self.notify)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Не удалось", self.sent[0])
        sync_channel(self.ch, fetch=lambda url: ics(), notify=self.notify)
        self.assertIn("снова работает", self.sent[-1])

    def test_html_instead_of_ical_is_error_without_cancellations(self):
        apply_feed(self.ch, [FeedEvent("1", d(5), d(7)), FeedEvent("2", d(9), d(10))])
        from .ical import fetch_feed  # noqa: F401 — проверяем через parse_feed

        r = sync_channel(self.ch, fetch=lambda url: b"<html>login</html>", notify=self.notify)
        self.assertFalse(r.ok)
        self.assertEqual(Booking.objects.active().count(), 2)


class ViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner", password="very-secret-pass")

    def test_login_required(self):
        resp = self.client.get(reverse("calendar"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp["Location"])

    def test_pages_render(self):
        self.client.force_login(self.user)
        ch = Channel.objects.create(name="Авито")
        Booking.objects.create(check_in=d(1), check_out=d(4), guest_name="Анна")
        Booking.objects.create(check_in=d(6), check_out=d(8), channel=ch, external_uid="u1")
        PriceRule.objects.create(name="Выходные", weekdays="4,5", price=15000)
        for name in ("calendar", "booking_list", "prices", "channels", "booking_create", "channel_create", "rule_create"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)
        resp = self.client.get(reverse("calendar"))
        self.assertContains(resp, "Анна")
        self.assertEqual(self.client.get(reverse("calendar") + "?m=2027-02").status_code, 200)
        self.assertEqual(self.client.get(reverse("calendar") + "?m=garbage").status_code, 200)

    def test_create_booking_and_overlap_error(self):
        self.client.force_login(self.user)
        data = {
            "kind": "booking",
            "check_in": d(10).isoformat(),
            "check_out": d(12).isoformat(),
            "guest_name": "Мария",
            "prepaid": 0,
            "total_price": 20000,
        }
        resp = self.client.post(reverse("booking_create"), data)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Booking.objects.count(), 1)
        data["check_in"] = d(11).isoformat()
        data["check_out"] = d(13).isoformat()
        resp = self.client.post(reverse("booking_create"), data)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Даты пересекаются")
        self.assertEqual(Booking.objects.count(), 1)

    def test_imported_booking_dates_locked(self):
        self.client.force_login(self.user)
        ch = Channel.objects.create(name="Авито")
        b = Booking.objects.create(check_in=d(6), check_out=d(8), channel=ch, external_uid="u1")
        resp = self.client.post(
            reverse("booking_edit", args=[b.pk]),
            {"check_in": d(1).isoformat(), "check_out": d(20).isoformat(), "guest_name": "Олег", "prepaid": 0},
        )
        self.assertEqual(resp.status_code, 302)
        b.refresh_from_db()
        self.assertEqual((b.check_in, b.check_out, b.guest_name, b.channel), (d(6), d(8), "Олег", ch))

    def test_quote_api(self):
        self.client.force_login(self.user)
        House.objects.update_or_create(pk=House.get().pk, defaults={"base_price": 5000})
        resp = self.client.get(reverse("quote_api"), {"check_in": d(1).isoformat(), "check_out": d(4).isoformat()})
        self.assertEqual(resp.json()["total"], 15000)
        self.assertTrue(resp.json()["available"])
        self.assertEqual(self.client.get(reverse("quote_api"), {"check_in": "x"}).status_code, 400)

    def test_period_price_form(self):
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("prices"),
            {
                "save_period": "1",
                "period-date_from": d(30).isoformat(),
                "period-date_to": d(35).isoformat(),
                "period-price": "18000",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(PriceCalculator().night_price(d(31)).price, 18000)

    def test_rule_form_weekdays(self):
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("rule_create"),
            {"name": "Выходные", "weekdays": ["4", "5"], "price": "15000", "priority": "10", "is_active": "on"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(PriceRule.objects.get().weekday_set, {4, 5})

    def test_ical_export_public_by_token(self):
        ch = Channel.objects.create(name="Авито")
        Booking.objects.create(check_in=d(1), check_out=d(3))
        resp = self.client.get(reverse("ical_export", args=[ch.export_token]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/calendar"))
        self.assertIn(b"BEGIN:VEVENT", resp.content)
        ch.refresh_from_db()
        self.assertIsNotNone(ch.last_exported_at)
        self.assertEqual(self.client.get(reverse("ical_export", args=["wrong"])).status_code, 404)

    def test_channel_sync_view(self):
        self.client.force_login(self.user)
        ch = Channel.objects.create(name="Авито")
        resp = self.client.post(reverse("channel_sync", args=[ch.pk]))
        self.assertEqual(resp.status_code, 302)  # без ссылки — сообщение об ошибке, без падения
