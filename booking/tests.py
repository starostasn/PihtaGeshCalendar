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


def G(name: str, phone: str = ""):
    """Гость для тестов (по имени; один и тот же для одинакового имени)."""
    from .models import Guest

    return Guest.objects.get_or_create(name=name, defaults={"phone": phone})[0]


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
        save_booking(Booking(check_in=d(10), check_out=d(13), guest=G("A")))
        save_booking(Booking(check_in=d(13), check_out=d(15), guest=G("B")))  # заезд в день выезда
        save_booking(Booking(check_in=d(8), check_out=d(10), guest=G("C")))
        with self.assertRaises(OverlapError):
            save_booking(Booking(check_in=d(12), check_out=d(14), guest=G("D")))
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
        Booking.objects.create(check_in=d(1), check_out=d(3), guest=G("Иван Петров", "+79001234567"))
        Booking.objects.create(check_in=d(5), check_out=d(7), channel=avito, external_uid="av-1")
        Booking.objects.create(check_in=d(9), check_out=d(10), channel=avito)  # вручную, источник Авито
        Booking.objects.create(check_in=d(20), check_out=d(22), status=Booking.Status.CANCELLED)

        for_avito = parse_feed(build_export(avito))
        self.assertEqual({(e.start, e.end) for e in for_avito}, {(d(1), d(3)), (d(9), d(10))})

        for_sutochno = parse_feed(build_export(sutochno))
        self.assertEqual(len(for_sutochno), 3)
        raw = build_export(sutochno).decode()
        self.assertNotIn("Иван", raw)
        self.assertNotIn("79001234567", raw)
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
        self.assertEqual(Booking.objects.get(external_uid="old").status, Booking.Status.CONFIRMED)

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
        save_booking(Booking(check_in=d(10), check_out=d(12), guest=G("Прямой")))
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
        self.user = User.objects.create_superuser("owner", password="very-secret-pass")

    def test_login_required(self):
        resp = self.client.get(reverse("calendar"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp["Location"])

    def test_pages_render(self):
        self.client.force_login(self.user)
        ch = Channel.objects.create(name="Авито")
        Booking.objects.create(check_in=d(1), check_out=d(4), guest=G("Анна"))
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
            "new_guest_name": "Мария",
            "new_guest_phone": "8 900 111-22-33",
            "status": "confirmed",
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
            {"check_in": d(1).isoformat(), "check_out": d(20).isoformat(), "new_guest_name": "Олег", "new_guest_phone": "+7 900 555-44-33", "status": "cancelled"},
        )
        self.assertEqual(resp.status_code, 302)
        b.refresh_from_db()
        self.assertEqual((b.check_in, b.check_out, b.guest.name, b.channel), (d(6), d(8), "Олег", ch))
        self.assertEqual(b.status, Booking.Status.CONFIRMED)  # статус импортированной брони задаёт площадка

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


# ---------------------------------------------------------------- статусы, платежи, статистика

from .models import Payment  # noqa: E402
from .stats import build_dashboard, resolve_period  # noqa: E402


class StatusTests(TestCase):
    def test_request_blocks_overlap_but_is_not_exported(self):
        ch = Channel.objects.create(name="Авито")
        save_booking(Booking(check_in=d(10), check_out=d(12), status=Booking.Status.REQUEST, guest=G("Заявка")))
        save_booking(Booking(check_in=d(20), check_out=d(22), guest=G("Бронь")))
        with self.assertRaises(OverlapError):
            save_booking(Booking(check_in=d(11), check_out=d(13)))
        exported = parse_feed(build_export(ch))
        self.assertEqual([(e.start, e.end) for e in exported], [(d(20), d(22))])

    def test_cancelled_restore_checks_overlap(self):
        a = save_booking(Booking(check_in=d(10), check_out=d(12), status=Booking.Status.CANCELLED))
        save_booking(Booking(check_in=d(10), check_out=d(12)))
        a.status = Booking.Status.CONFIRMED
        with self.assertRaises(OverlapError):
            save_booking(a)

    def test_block_cannot_be_request(self):
        from .forms import BookingForm

        form = BookingForm(
            data={"kind": "block", "status": "request", "check_in": d(1).isoformat(), "check_out": d(2).isoformat()}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("status", form.errors)

    def test_imported_booking_reactivated_as_confirmed(self):
        ch = Channel.objects.create(name="Авито", import_url="https://example.com/a.ics")
        apply_feed(ch, [FeedEvent("1", d(5), d(7))])
        b = Booking.objects.get(external_uid="1")
        self.assertEqual(b.status, Booking.Status.CONFIRMED)
        b.status = Booking.Status.CANCELLED
        b.save()
        apply_feed(ch, [FeedEvent("1", d(5), d(7))])
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.CONFIRMED)


class PaymentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("owner", password="very-secret-pass")
        self.client.force_login(self.user)

    def test_balance_and_states(self):
        b = Booking.objects.create(check_in=d(1), check_out=d(3), total_price=20000)
        self.assertEqual((b.paid, b.balance_due, b.payment_state), (0, 20000, "unpaid"))
        Payment.objects.create(booking=b, amount=6000)
        self.assertEqual((b.paid, b.balance_due, b.payment_state, b.paid_percent), (6000, 14000, "partial", 30))
        Payment.objects.create(booking=b, amount=16000)
        self.assertEqual((b.balance_due, b.payment_state), (-2000, "overpaid"))
        Payment.objects.create(booking=b, amount=2000, kind=Payment.Kind.REFUND)
        self.assertEqual((b.paid, b.balance_due, b.payment_state), (20000, 0, "paid"))
        self.assertEqual(Booking.objects.create(check_in=d(5), check_out=d(6)).payment_state, "unknown")

    def test_create_with_prepayment_add_and_delete_payment(self):
        resp = self.client.post(
            reverse("booking_create"),
            {
                "kind": "booking",
                "status": "request",
                "check_in": d(10).isoformat(),
                "check_out": d(12).isoformat(),
                "total_price": 20000,
                "initial_payment": 5000,
                "initial_payment_method": "sbp",
            },
        )
        self.assertEqual(resp.status_code, 302)
        b = Booking.objects.get()
        self.assertEqual((b.status, b.paid), (Booking.Status.REQUEST, 5000))
        self.assertEqual(b.payments.get().method, "sbp")

        resp = self.client.post(
            reverse("payment_add", args=[b.pk]),
            {"date": d(0).isoformat(), "kind": "payment", "amount": 15000, "method": "cash"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(b.balance_due, 0)
        page = self.client.get(reverse("booking_edit", args=[b.pk]))
        self.assertContains(page, "Оплачено")

        bad = self.client.post(reverse("payment_add", args=[b.pk]), {"date": d(0).isoformat(), "kind": "payment", "amount": 0, "method": "cash"})
        self.assertEqual(bad.status_code, 200)  # ошибка формы, страница брони с ошибкой
        self.assertEqual(b.payments.count(), 2)

        p = b.payments.order_by("pk").last()
        self.client.post(reverse("payment_delete", args=[p.pk]))
        self.assertEqual(b.payments.count(), 1)

    def test_lists_render_with_payments(self):
        b = Booking.objects.create(check_in=d(1), check_out=d(3), total_price=20000, guest=G("Должник"))
        Payment.objects.create(booking=b, amount=1000)
        for show in ("upcoming", "requests", "debts", "past", "cancelled"):
            self.assertEqual(self.client.get(reverse("booking_list"), {"show": show}).status_code, 200, show)
        self.assertContains(self.client.get(reverse("booking_list"), {"show": "debts"}), "Должник")
        self.assertContains(self.client.get(reverse("calendar")), "к оплате")


class StatsTests(TestCase):
    def test_revenue_split_by_nights_and_occupancy(self):
        from datetime import date as _date

        avito = Channel.objects.create(name="Авито", commission_percent=10)
        # 3 ночи: 30.01, 31.01, 01.02 — две ночи в январе, одна в феврале
        Booking.objects.create(check_in=_date(2025, 1, 30), check_out=_date(2025, 2, 2), total_price=30000, channel=avito)
        Booking.objects.create(check_in=_date(2025, 1, 10), check_out=_date(2025, 1, 12), total_price=10000)
        Booking.objects.create(check_in=_date(2025, 1, 15), check_out=_date(2025, 1, 17), total_price=99999,
                               status=Booking.Status.CANCELLED)
        Booking.objects.create(check_in=_date(2025, 1, 20), check_out=_date(2025, 1, 21), total_price=5000,
                               status=Booking.Status.REQUEST)
        Booking.objects.create(kind=Booking.Kind.BLOCK, check_in=_date(2025, 1, 1), check_out=_date(2025, 1, 6))

        period = resolve_period("custom", _date(2025, 1, 1), _date(2025, 1, 31), _date(2025, 3, 1))
        dash = build_dashboard(period, _date(2025, 3, 1))
        k = dash.kpi
        self.assertEqual(k["revenue"], 30000)  # 20000 (2 ночи из 3) + 10000
        self.assertEqual(k["commission"], 2000)
        self.assertEqual((k["occupied"], k["available"], k["blocked"]), (4, 26, 5))
        self.assertAlmostEqual(k["occupancy"], 4 / 26 * 100)
        self.assertEqual(k["adr"], 7500)
        self.assertEqual((k["arrivals"], k["cancelled"]), (2, 1))
        self.assertAlmostEqual(k["cancel_rate"], 100 / 3)
        self.assertEqual((dash.requests_count, dash.requests_nights), (1, 1))
        self.assertEqual([s["name"] for s in dash.sources], ["Авито", "Напрямую"])
        self.assertEqual(dash.sources[0]["net"], 18000)
        self.assertEqual(dash.granularity, "week")
        self.assertEqual(sum(s["revenue"] for s in dash.series), 30000)

    def test_presets(self):
        from datetime import date as _date

        today = _date(2026, 3, 15)
        self.assertEqual(resolve_period("this_month", None, None, today).start, _date(2026, 3, 1))
        lm = resolve_period("last_month", None, None, today)
        self.assertEqual((lm.start, lm.end), (_date(2026, 2, 1), _date(2026, 3, 1)))
        self.assertEqual(lm.previous().start, _date(2026, 1, 1))
        y = resolve_period("this_year", None, None, today)
        self.assertEqual((y.start, y.end, y.previous().start), (_date(2026, 1, 1), _date(2027, 1, 1), _date(2025, 1, 1)))
        self.assertEqual(resolve_period("last_12", None, None, today).start, _date(2025, 4, 1))
        self.assertEqual(resolve_period("custom", None, None, today).preset, "this_month")  # без дат — текущий месяц

    def test_stats_page_renders_for_all_presets(self):
        self.client.force_login(User.objects.create_user("owner", password="very-secret-pass"))
        b = Booking.objects.create(check_in=d(-3), check_out=d(2), total_price=50000, guest=G("Гость"))
        Payment.objects.create(booking=b, amount=10000)
        for preset in ("this_month", "last_month", "next_90", "last_12", "this_year", "last_year", "garbage"):
            resp = self.client.get(reverse("stats"), {"period": preset})
            self.assertEqual(resp.status_code, 200, preset)
        resp = self.client.get(reverse("stats"), {"period": "custom", "from": d(-10).isoformat(), "to": d(10).isoformat()})
        self.assertContains(resp, "Ждём оплату")
        self.assertContains(resp, "chart-data")



# ---------------------------------------------------------------- пользователи и роли

from django.core.cache import cache  # noqa: E402
from django.test import override_settings  # noqa: E402

from .models import Invite  # noqa: E402

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class RoleTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", password="very-secret-pass")
        self.manager = User.objects.create_user("manager", password="very-secret-pass", first_name="Ирина")
        self.ch = Channel.objects.create(name="Авито")
        self.rule = PriceRule.objects.create(name="Выходные", weekdays="4,5", price=15000)

    def test_manager_can_work_with_bookings_and_stats(self):
        self.client.force_login(self.manager)
        for name in ("calendar", "booking_list", "booking_create", "stats", "prices", "account"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        resp = self.client.post(
            reverse("booking_create"),
            {"kind": "booking", "status": "confirmed", "check_in": d(5).isoformat(), "check_out": d(7).isoformat(),
             "total_price": 20000, "initial_payment": 5000, "initial_payment_method": "cash"},
        )
        self.assertEqual(resp.status_code, 302)
        b = Booking.objects.get()
        self.assertEqual(b.created_by, self.manager)
        self.assertEqual(b.payments.get().created_by, self.manager)
        self.client.post(reverse("payment_add", args=[b.pk]), {"date": d(0).isoformat(), "kind": "payment", "amount": 1000, "method": "cash"})
        self.assertEqual(b.payments.count(), 2)
        page = self.client.get(reverse("booking_edit", args=[b.pk]))
        self.assertContains(page, "Ирина")
        self.assertNotContains(page, "Удалить платёж")

    def test_manager_cannot_change_prices_channels_users(self):
        self.client.force_login(self.manager)
        page = self.client.get(reverse("prices"))
        self.assertContains(page, "Цены меняет администратор")
        self.assertNotContains(page, "Изменить цену на период")
        forbidden = [
            ("post", reverse("prices"), {"save_period": "1", "period-date_from": d(1).isoformat(),
                                         "period-date_to": d(2).isoformat(), "period-price": "1"}),
            ("get", reverse("rule_create"), None),
            ("get", reverse("rule_edit", args=[self.rule.pk]), None),
            ("post", reverse("rule_delete", args=[self.rule.pk]), None),
            ("get", reverse("channels"), None),
            ("get", reverse("channel_edit", args=[self.ch.pk]), None),
            ("post", reverse("channel_sync", args=[self.ch.pk]), None),
            ("get", reverse("users"), None),
            ("get", reverse("user_create"), None),
            ("get", reverse("user_edit", args=[self.admin.pk]), None),
            ("post", reverse("invite_create"), {"name": "x", "role": "admin"}),
        ]
        for method, url, data in forbidden:
            resp = getattr(self.client, method)(url, data or {})
            self.assertEqual(resp.status_code, 403, url)
        b = Booking.objects.create(check_in=d(1), check_out=d(2), total_price=100)
        p = Payment.objects.create(booking=b, amount=100)
        self.assertEqual(self.client.post(reverse("payment_delete", args=[p.pk])).status_code, 403)
        self.assertTrue(Payment.objects.filter(pk=p.pk).exists())
        self.assertEqual(PriceRule.objects.count(), 1)
        self.assertEqual(Invite.objects.count(), 0)
        nav = self.client.get(reverse("calendar"))
        self.assertNotContains(nav, reverse("users"))
        self.assertNotContains(nav, reverse("channels"))

    def test_admin_sees_admin_sections(self):
        self.client.force_login(self.admin)
        nav = self.client.get(reverse("calendar"))
        self.assertContains(nav, reverse("users"))
        self.assertContains(nav, reverse("channels"))
        for name in ("users", "user_create", "channels"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)


class UserManagementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", password="very-secret-pass")
        self.client.force_login(self.admin)

    def test_create_edit_block_and_reset_password(self):
        resp = self.client.post(reverse("user_create"), {
            "username": "irina", "first_name": "Ирина", "role": "manager",
            "password1": "Long-pass-12345", "password2": "Long-pass-12345",
        })
        self.assertEqual(resp.status_code, 302)
        u = User.objects.get(username="irina")
        self.assertFalse(u.is_superuser)
        self.assertTrue(u.check_password("Long-pass-12345"))

        self.client.post(reverse("user_edit", args=[u.pk]), {"first_name": "Ирина П.", "role": "admin", "is_active": "on"})
        u.refresh_from_db()
        self.assertTrue(u.is_superuser and u.is_staff)
        self.assertEqual(u.first_name, "Ирина П.")

        self.client.post(reverse("user_edit", args=[u.pk]), {"first_name": "Ирина П.", "role": "manager"})
        u.refresh_from_db()
        self.assertFalse(u.is_active or u.is_superuser)  # снята галочка — заблокирован

        self.client.post(reverse("user_edit", args=[u.pk]), {"set_password": "1", "new_password1": "Another-pass-777", "new_password2": "Another-pass-777"})
        u.refresh_from_db()
        self.assertTrue(u.check_password("Another-pass-777"))

    def test_blocked_user_is_logged_out(self):
        u = User.objects.create_user("worker", password="very-secret-pass")
        other = self.client_class()
        other.force_login(u)
        self.assertEqual(other.get(reverse("calendar")).status_code, 200)
        u.is_active = False
        u.save()
        self.assertEqual(other.get(reverse("calendar")).status_code, 302)

    def test_admin_cannot_demote_or_block_self(self):
        self.client.post(reverse("user_edit", args=[self.admin.pk]), {"first_name": "Я", "role": "manager"})
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_superuser and self.admin.is_active)
        self.assertEqual(self.admin.first_name, "Я")

    def test_weak_password_rejected(self):
        resp = self.client.post(reverse("user_create"), {"username": "x", "role": "manager", "password1": "123", "password2": "123"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="x").exists())


class InviteTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", password="very-secret-pass")

    def accept(self, invite, username="newbie"):
        return self.client.post(reverse("invite_accept", args=[invite.token]), {
            "first_name": "Новенький", "username": username,
            "password1": "Long-pass-12345", "password2": "Long-pass-12345",
        })

    def test_create_accept_and_reuse(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("invite_create"), {"name": "Ирина, менеджер", "role": "manager"})
        invite = Invite.objects.get()
        self.assertEqual(invite.created_by, self.admin)
        self.assertContains(self.client.get(reverse("users")), invite.token)
        self.client.logout()

        page = self.client.get(reverse("invite_accept", args=[invite.token]))
        self.assertContains(page, "Вас пригласили")
        self.assertContains(page, 'value="Ирина"')
        resp = self.accept(invite)
        self.assertEqual(resp.status_code, 302)
        user = User.objects.get(username="newbie")
        self.assertFalse(user.is_superuser)
        invite.refresh_from_db()
        self.assertEqual((invite.status, invite.used_by), ("used", user))
        self.assertEqual(self.client.get(reverse("calendar")).status_code, 200)  # сразу вошёл

        self.client.logout()
        self.assertEqual(self.client.get(reverse("invite_accept", args=[invite.token])).status_code, 410)
        self.accept(invite, username="second")
        self.assertFalse(User.objects.filter(username="second").exists())

    def test_admin_invite_expired_and_revoked(self):
        admin_inv = Invite.objects.create(role="admin")
        self.accept(admin_inv, "boss")
        self.assertTrue(User.objects.get(username="boss").is_superuser)
        self.client.logout()

        expired = Invite.objects.create(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.get(reverse("invite_accept", args=[expired.token])).status_code, 410)
        revoked = Invite.objects.create()
        self.client.force_login(self.admin)
        self.client.post(reverse("invite_revoke", args=[revoked.pk]))
        self.client.logout()
        revoked.refresh_from_db()
        self.assertEqual(revoked.status, "revoked")
        self.accept(revoked, "late")
        self.assertFalse(User.objects.filter(username="late").exists())
        self.assertEqual(self.client.get(reverse("invite_accept", args=["nope"])).status_code, 404)


@override_settings(CACHES=LOCMEM)
class LoginThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_user("owner", password="very-secret-pass")

    def test_lockout_after_failed_attempts(self):
        for _ in range(5):
            resp = self.client.post(reverse("login"), {"username": "owner", "password": "wrong"})
            self.assertEqual(resp.status_code, 200)
        resp = self.client.post(reverse("login"), {"username": "owner", "password": "very-secret-pass"})
        self.assertEqual(resp.status_code, 429)
        self.assertContains(resp, "Слишком много", status_code=429)
        cache.clear()
        resp = self.client.post(reverse("login"), {"username": "owner", "password": "very-secret-pass"})
        self.assertEqual(resp.status_code, 302)

    def test_profile_name_and_password(self):
        self.client.post(reverse("login"), {"username": "owner", "password": "very-secret-pass"})
        self.client.post(reverse("account"), {"first_name": "Сергей"})
        self.assertEqual(User.objects.get(username="owner").first_name, "Сергей")
        resp = self.client.post(reverse("account"), {"change_password": "1", "old_password": "very-secret-pass",
                                                     "new_password1": "Brand-new-pass-1", "new_password2": "Brand-new-pass-1"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get(reverse("calendar")).status_code, 200)  # сессия сохранилась
        self.assertTrue(User.objects.get(username="owner").check_password("Brand-new-pass-1"))


# ---------------------------------------------------------------- заголовки безопасности и /admin/


class SecurityHeadersTests(TestCase):
    def test_headers_present(self):
        resp = self.client.get(reverse("login"))
        csp = resp.headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("camera=()", resp.headers["Permissions-Policy"])
        self.assertEqual(resp.headers["X-Frame-Options"], "DENY")
        self.assertEqual(resp.headers["Referrer-Policy"], "same-origin")
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")

    def test_no_inline_scripts_in_pages(self):
        import re

        admin = User.objects.create_superuser("admin", password="very-secret-pass")
        self.client.force_login(admin)
        ch = Channel.objects.create(name="Авито")
        b = Booking.objects.create(check_in=d(1), check_out=d(3), total_price=1000)
        Payment.objects.create(booking=b, amount=100)
        Invite.objects.create(name="Тест")
        PriceRule.objects.create(name="Выходные", weekdays="4,5", price=1)
        urls = [reverse(n) for n in ("calendar", "booking_list", "prices", "stats", "channels", "users", "account")]
        urls += [reverse("booking_edit", args=[b.pk]), reverse("channel_edit", args=[ch.pk])]
        for url in urls:
            html = self.client.get(url).content.decode()
            self.assertNotRegex(html, r"\son(click|submit|change|load)=", url)
            for tag in re.findall(r"<script[^>]*>", html):
                self.assertTrue("src=" in tag or 'type="application/json"' in tag, (url, tag))

    def test_django_admin_disabled_by_default(self):
        self.assertEqual(self.client.get("/admin/").status_code, 404)


# ---------------------------------------------------------------- гости

from .models import Guest  # noqa: E402
from .phones import format_phone, normalize_phone  # noqa: E402


class PhoneTests(TestCase):
    def test_normalize_and_format(self):
        for raw in ("8 (912) 345-67-89", "+7 912 345 67 89", "79123456789", "9123456789", " +7(912)3456789 "):
            self.assertEqual(normalize_phone(raw), "+79123456789", raw)
        self.assertEqual(normalize_phone("+375 29 123-45-67"), "+375291234567")
        self.assertEqual(normalize_phone("12345"), "")
        self.assertEqual(normalize_phone(""), "")
        self.assertEqual(format_phone("+79123456789"), "+7 912 345-67-89")


class GuestTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", password="very-secret-pass")
        self.manager = User.objects.create_user("manager", password="very-secret-pass")
        self.client.force_login(self.manager)

    def post_booking(self, a, b, **extra):
        data = {"kind": "booking", "status": "confirmed", "check_in": d(a).isoformat(), "check_out": d(b).isoformat(), "total_price": 10000}
        data.update(extra)
        return self.client.post(reverse("booking_create"), data)

    def test_booking_creates_guest_and_reuses_by_phone(self):
        self.post_booking(10, 12, new_guest_name="Анна Смирнова", new_guest_phone="8 (912) 345-67-89")
        g = Guest.objects.get()
        self.assertEqual((g.name, g.phone, g.created_by), ("Анна Смирнова", "+79123456789", self.manager))
        # тот же номер в другом написании — та же гостья, даже если имя написано иначе
        self.post_booking(20, 22, new_guest_name="Аня", new_guest_phone="+7 912 3456789")
        self.assertEqual(Guest.objects.count(), 1)
        self.assertEqual(g.bookings.count(), 2)
        # выбор из справочника
        self.post_booking(30, 32, guest=g.pk)
        self.assertEqual(g.bookings.count(), 3)

    def test_guest_validation_and_no_orphans(self):
        resp = self.post_booking(10, 12, new_guest_name="Без телефона")
        self.assertContains(resp, "Укажите телефон")
        resp = self.post_booking(10, 12, new_guest_phone="+7 900 000-00-01")
        self.assertContains(resp, "Укажите имя")
        resp = self.post_booking(10, 12, new_guest_name="Кривой", new_guest_phone="123")
        self.assertContains(resp, "Не похоже на номер")
        self.assertEqual(Guest.objects.count(), 0)
        save_booking(Booking(check_in=d(10), check_out=d(12)))
        resp = self.post_booking(11, 13, new_guest_name="Олег", new_guest_phone="+7 900 000-00-02")
        self.assertContains(resp, "Даты пересекаются")
        self.assertEqual(Guest.objects.count(), 0)  # бронь не сохранилась — гость не создан
        self.post_booking(20, 21)  # бронь без гостя допустима
        self.assertTrue(Booking.objects.filter(check_in=d(20), guest=None).exists())

    def test_list_search_and_card(self):
        g1 = Guest.objects.create(name="Анна Смирнова", phone="+79123456789", city="Казань")
        Guest.objects.create(name="Пётр Иванов", phone="+79990001122")
        Guest.objects.create(name="Без Телефона")
        Booking.objects.create(check_in=d(-10), check_out=d(-7), guest=g1, total_price=30000)
        Booking.objects.create(check_in=d(5), check_out=d(7), guest=g1, total_price=20000)
        Booking.objects.create(check_in=d(15), check_out=d(17), guest=g1, total_price=999, status=Booking.Status.CANCELLED)
        for q, expected in (("анна", 1), ("АННА смир", 1), ("912 345", 1), ("8912", 1), ("казань", 1), ("", 3), ("нет такого", 0)):
            resp = self.client.get(reverse("guests"), {"q": q})
            self.assertEqual(len(resp.context["page"].object_list), expected, q)
        resp = self.client.get(reverse("guests"), {"sort": "total"})
        self.assertEqual(resp.context["page"].object_list[0], g1)
        self.assertEqual(resp.context["page"].object_list[0].total, 50000)  # отмена не считается
        self.assertContains(self.client.get(reverse("guests")), "нет телефона")
        card = self.client.get(reverse("guest_edit", args=[g1.pk]))
        self.assertEqual(card.context["summary"]["count"], 2)
        self.assertEqual(card.context["summary"]["total"], 50000)
        api = self.client.get(reverse("guest_search"), {"q": "смирн"}).json()["results"]
        self.assertEqual([r["name"] for r in api], ["Анна Смирнова"])
        self.assertEqual(api[0]["phone"], "+7 912 345-67-89")

    def test_create_edit_and_duplicate_phone(self):
        self.client.post(reverse("guest_create"), {"name": "Ирина", "phone": "8 900 123-45-67"})
        g = Guest.objects.get(name="Ирина")
        self.assertEqual(g.phone, "+79001234567")
        resp = self.client.post(reverse("guest_create"), {"name": "Двойник", "phone": "+7 900 1234567"})
        self.assertContains(resp, "уже есть у гостя")
        resp = self.client.post(reverse("guest_create"), {"name": "Без номера"})
        self.assertContains(resp, "Укажите телефон")
        legacy = Guest.objects.create(name="Старый гость")  # из импорта, без телефона
        resp = self.client.post(reverse("guest_edit", args=[legacy.pk]), {"name": "Старый гость", "city": "Пермь"})
        self.assertEqual(resp.status_code, 302)

    def test_merge_and_delete_admin_only(self):
        keep = Guest.objects.create(name="Анна", city="")
        dup = Guest.objects.create(name="Анна С.", phone="+79123456789", city="Казань", notes="любит тишину")
        Booking.objects.create(check_in=d(1), check_out=d(2), guest=dup)
        self.assertEqual(self.client.post(reverse("guest_merge", args=[keep.pk]), {"other": dup.pk}).status_code, 403)
        self.client.force_login(self.admin)
        self.client.post(reverse("guest_merge", args=[keep.pk]), {"other": dup.pk})
        keep.refresh_from_db()
        self.assertFalse(Guest.objects.filter(pk=dup.pk).exists())
        self.assertEqual((keep.phone, keep.city, keep.notes), ("+79123456789", "Казань", "любит тишину"))
        self.assertEqual(keep.bookings.count(), 1)
        self.client.post(reverse("guest_delete", args=[keep.pk]))
        self.assertTrue(Guest.objects.filter(pk=keep.pk).exists())  # есть брони — не удаляется
        empty = Guest.objects.create(name="Пустой")
        self.client.post(reverse("guest_delete", args=[empty.pk]))
        self.assertFalse(Guest.objects.filter(pk=empty.pk).exists())


# ---------------------------------------------------------------- загрузка старых броней

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402

from .importer import ImportFileError, parse_contacts, parse_file, parse_guests_count  # noqa: E402
from .models import ImportBatch  # noqa: E402


def rc_xls(rows, header=("Объект", "Заезд", "Выезд", "Контакты", "Примечания", "Гостей", "Сумма", "Источник", "Менеджер")) -> bytes:
    """Файл в формате выгрузки RealtyCalendar (Excel XML 2003) с выдуманными данными."""
    def row(cells):
        out = []
        for c in cells:
            kind = "Number" if isinstance(c, float) else "String"
            out.append(f'<Cell><Data ss:Type="{kind}">{c}</Data></Cell>')
        return "<Row>" + "".join(out) + "</Row>"
    body = row(header) + "".join(row(r) for r in rows)
    return (
        '<?xml version="1.0"?>\n<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
        'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="Sheet1"><Table>'
        + body + "</Table></Worksheet></Workbook>"
    ).encode()


def fmt(x):
    return x.strftime("%d.%m.%Y")


class ImportParsingTests(TestCase):
    def test_contacts_and_guests(self):
        self.assertEqual(parse_contacts("Анна Смирнова +7 912 345-67-89"), ("Анна Смирнова", "+79123456789"))
        self.assertEqual(parse_contacts("Пётр И. А., 01.02.1980 8 (900) 111-22-33"), ("Пётр И. А.", "+79001112233"))
        self.assertEqual(parse_contacts("Только Имя "), ("Только Имя", ""))
        self.assertEqual(parse_contacts(""), ("", ""))
        self.assertEqual(parse_guests_count("", "4 гостей."), 4)
        self.assertEqual(parse_guests_count("", "Гостей: 3"), 3)
        self.assertEqual(parse_guests_count("", "2 взрослых и ребенок"), 3)
        self.assertEqual(parse_guests_count("", "3 человека + 2 реб"), 5)
        self.assertEqual(parse_guests_count("5", "что угодно"), 5)
        self.assertIsNone(parse_guests_count("", "жили у соседа"))

    def test_bad_files(self):
        with self.assertRaises(ImportFileError):
            parse_file(b"PK\x03\x04 not xml")
        with self.assertRaises(ImportFileError):
            parse_file(rc_xls([("x",)], header=("Объект", "Контакты")))
        with self.assertRaises(ImportFileError):
            parse_file(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><Workbook/>')
        rows = parse_file(rc_xls([("Дом", "31.02.2025", "03.03.2025", "", "", "", 1.0, "", ""),
                                  ("Дом", "05.03.2025", "04.03.2025", "", "", "", 1.0, "", "")]))
        self.assertEqual([r["error"] for r in rows], ["не распознаны даты", "выезд не позже заезда"])


class ImportFlowTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", password="very-secret-pass")
        self.client.force_login(self.admin)
        self.avito = Channel.objects.create(name="Авито")
        self.file = rc_xls([
            ("Дом", fmt(d(-100)), fmt(d(-97)), "Анна Смирнова +7 912 345-67-89", "4 гостей.", "", 30000.0, "manual", "s@x.ru"),
            ("Дом", fmt(d(-50)), fmt(d(-48)), "Анна С. 8 912 3456789", "Гостей: 2", "", 20000.0, "avito.ru", "s@x.ru"),
            ("Дом", fmt(d(-30)), fmt(d(-28)), "Без Телефона", "", "", 15000.0, "YandexTravel", "p@x.ru"),
            ("Дом", fmt(d(-20)), fmt(d(-19)), "", "", "", 0.0, "widget", ""),
            ("Дом", fmt(d(10)), fmt(d(14)), "Пётр +7 900 000-00-07", "", "", 40000.0, "Avito", "a@x.ru"),
            ("Дом", fmt(d(30)), fmt(d(33)), "Конфликт +7 900 000-00-08", "", "", 1.0, "manual", ""),
        ])
        # бронь с Авито по iCal на даты строки 5 (без гостя) и ручная бронь, пересекающая строку 6
        self.ical = Booking.objects.create(check_in=d(10), check_out=d(14), channel=self.avito, external_uid="u1")
        Booking.objects.create(check_in=d(31), check_out=d(35), guest=G("Живой гость"))

    def upload(self):
        resp = self.client.post(reverse("import"), {"file": SimpleUploadedFile("bookings.xls", self.file)})
        self.assertEqual(resp.status_code, 302)
        return ImportBatch.objects.latest("pk")

    def test_preview_apply_revert(self):
        batch = self.upload()
        page = self.client.get(reverse("import_preview", args=[batch.pk]))
        s = page.context["summary"]
        self.assertEqual((s["create"], s["enrich"], s["conflict"], s["new_guests"], s["no_phone"], s["blocks"]), (4, 1, 1, 3, 1, 1))
        self.assertEqual(Booking.objects.count(), 2)  # предпросмотр ничего не создаёт

        mapping = {src["index"]: src for src in page.context["sources"]}
        post = {"action": "apply", "mark_paid": "on", "zero_as_block": "on"}
        for i, src in mapping.items():
            post[f"source_{i}"] = src["value"]
        self.client.post(reverse("import_preview", args=[batch.pk]), post)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "applied")
        self.assertEqual(batch.result["created"], 4)

        anna = Guest.objects.get(phone="+79123456789")
        self.assertEqual(anna.bookings.count(), 2)  # одна гостья по телефону в разных написаниях
        nophone = Guest.objects.get(name="Без Телефона")
        self.assertEqual(nophone.phone, "")
        first = Booking.objects.get(check_in=d(-100))
        self.assertEqual((first.guests, first.total_price, first.channel), (4, 30000, None))
        self.assertIn("менеджер: s@x.ru", first.comment)
        self.assertEqual(first.payments.get().amount, 30000)  # прошлая бронь оплачена
        self.assertEqual(Booking.objects.get(check_in=d(-50)).channel, self.avito)
        self.assertEqual(Booking.objects.get(check_in=d(-30)).channel.name, "Яндекс Путешествия")
        blocked = Booking.objects.get(check_in=d(-20))  # без гостя и с нулём — закрытые даты
        self.assertEqual((blocked.kind, blocked.guest, blocked.payments.count()), ("block", None, 0))
        self.ical.refresh_from_db()
        self.assertEqual((self.ical.guest.name, self.ical.total_price, self.ical.payments.count()), ("Пётр", 40000, 0))
        self.assertFalse(Booking.objects.filter(check_in=d(30)).exists())  # конфликт пропущен

        self.client.post(reverse("import_revert", args=[batch.pk]))
        batch.refresh_from_db()
        self.ical.refresh_from_db()
        self.assertEqual(batch.status, "reverted")
        self.assertEqual(Booking.objects.count(), 2)
        self.assertIsNone(self.ical.guest)
        self.assertIsNone(self.ical.total_price)
        self.assertFalse(Guest.objects.filter(import_batch=batch).exists())
        self.assertFalse(Channel.objects.filter(name="Яндекс Путешествия").exists())
        self.assertEqual(Payment.objects.count(), 0)

    def test_reupload_is_idempotent(self):
        from .importer import apply_batch

        b1 = self.upload()
        apply_batch(b1, self.admin)
        counts = (Booking.objects.count(), Guest.objects.count(), Payment.objects.count())
        b2 = self.upload()
        s = self.client.get(reverse("import_preview", args=[b2.pk])).context["summary"]
        self.assertEqual((s["create"], s["enrich"], s["same"]), (0, 0, 5))
        apply_batch(b2, self.admin)
        self.assertEqual((Booking.objects.count(), Guest.objects.count(), Payment.objects.count()), counts)

    def test_manager_cannot_import(self):
        self.client.force_login(User.objects.create_user("m", password="very-secret-pass"))
        self.assertEqual(self.client.get(reverse("import")).status_code, 403)
        self.assertEqual(self.client.post(reverse("import"), {"file": SimpleUploadedFile("b.xls", self.file)}).status_code, 403)


# ---------------------------------------------------------------- о доме и фото

import importlib  # noqa: E402
import io  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from PIL import Image  # noqa: E402

from .models import HousePhoto  # noqa: E402


def make_jpeg(w=3000, h=2000, orientation=None, gps=True) -> bytes:
    img = Image.new("RGB", (w, h), (40, 90, 60))
    exif = Image.Exif()
    exif[0x010F] = "TestCam"  # Make
    if orientation:
        exif[0x0112] = orientation
    if gps:
        exif[0x8825] = {1: "N", 2: (53.0, 0.0, 0.0), 3: "E", 4: (87.0, 0.0, 0.0)}  # GPSInfo
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


class HouseInfoTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", password="very-secret-pass")
        self.manager = User.objects.create_user("manager", password="very-secret-pass")

    def test_listing_migration_fills_only_empty_fields(self):
        mod = importlib.import_module("booking.migrations.0007_fill_house_from_listing")
        from django.apps import apps

        house = House.get()
        house.rules = "Свои правила"
        house.save()
        mod.fill(apps, None)
        house.refresh_from_db()
        self.assertEqual((house.name, house.max_guests, house.area_m2, house.prepayment_percent), ("ПихтаГеш", 8, 80, 30))
        self.assertEqual(house.check_in_time.strftime("%H:%M"), "15:00")
        self.assertIn("Славянская", house.address)
        self.assertEqual(house.rules, "Свои правила")  # заполненное вручную не трогаем
        house.name = "Моё название"
        house.save()
        mod.fill(apps, None)
        house.refresh_from_db()
        self.assertEqual(house.name, "Моё название")

    def test_page_for_team_and_edit_for_admin(self):
        house = House.get()
        house.amenities = "Wi-Fi\nСушилка для ботинок"
        house.wifi_password = "pihta-2026"
        house.admin_notes = "секрет админа"
        house.address = "пгт. Шерегеш, Славянская ул., 53"
        house.save()
        self.client.force_login(self.manager)
        page = self.client.get(reverse("house"))
        self.assertContains(page, "Сушилка для ботинок")
        self.assertContains(page, "pihta-2026")  # команде виден пароль Wi-Fi
        self.assertNotContains(page, "секрет админа")
        self.assertContains(page, "yandex.ru/maps/?text=")
        self.assertNotContains(page, reverse("house_edit"))
        self.assertEqual(self.client.get(reverse("house_edit")).status_code, 403)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("house")), "секрет админа")
        data = {f: "" for f in ("tagline", "description", "address", "avito_url", "floors", "layout", "amenities",
                                 "location_info", "rules", "cancellation_policy", "extra_services",
                                 "checkin_instructions", "wifi_name", "wifi_password", "lockbox_code",
                                 "team_contacts", "admin_notes", "area_m2", "plot_sotok", "rooms", "deposit")}
        data.update(name="ПихтаГеш", max_guests=8, check_in_time="15:00", check_out_time="12:00", prepayment_percent=30,
                    lockbox_code="4321")
        resp = self.client.post(reverse("house_edit"), data)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(House.get().lockbox_code, "4321")

    def test_booking_form_shows_prepayment_percent(self):
        house = House.get()
        house.prepayment_percent = 30
        house.save()
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(reverse("booking_create")), 'data-prepay="30"')


class HousePhotoTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = self.settings(DATA_DIR=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        self.admin = User.objects.create_superuser("admin", password="very-secret-pass")
        self.client.force_login(self.admin)

    def upload(self, *files):
        return self.client.post(reverse("photo_upload"), {"photos": list(files)})

    def test_upload_strips_metadata_resizes_and_rotates(self):
        self.upload(SimpleUploadedFile("Пихта — дом зимой.jpg", make_jpeg(3000, 2000, orientation=6), "image/jpeg"))
        photo = HousePhoto.objects.get()
        self.assertTrue(photo.is_cover)  # первое фото становится главным
        full = Path(self.tmp.name) / "media" / photo.image
        with Image.open(full) as img:
            self.assertEqual(max(img.size), 2000)
            self.assertEqual(img.size, (1333, 2000))  # ориентация 6 — повёрнуто
            exif = img.getexif()
            self.assertNotIn(0x8825, exif)  # геометки нет
            self.assertNotIn(0x010F, exif)  # и других метаданных
        self.assertTrue((Path(self.tmp.name) / "media" / photo.thumb).is_file())

    def test_rejects_non_images_and_requires_login(self):
        resp = self.upload(SimpleUploadedFile("virus.jpg", b"not an image at all", "image/jpeg"))
        self.assertEqual(HousePhoto.objects.count(), 0)
        self.assertContains(self.client.get(reverse("house")), "не фотография")
        self.upload(SimpleUploadedFile("a.jpg", make_jpeg(800, 600), "image/jpeg"))
        photo = HousePhoto.objects.get()
        url = reverse("photo_file", args=[photo.pk, "thumb"])
        self.assertEqual(self.client.get(url)["Content-Type"], "image/jpeg")
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)  # без входа фото не отдаются
        manager = User.objects.create_user("m", password="very-secret-pass")
        self.client.force_login(manager)
        self.assertEqual(self.client.get(url).status_code, 200)  # команде видно
        self.assertEqual(self.upload(SimpleUploadedFile("b.jpg", make_jpeg(800, 600), "image/jpeg")).status_code, 403)
        self.assertEqual(self.client.post(reverse("photo_update", args=[photo.pk]), {"action": "delete"}).status_code, 403)

    def test_order_cover_caption_delete(self):
        self.upload(*[SimpleUploadedFile(f"{i}.jpg", make_jpeg(800, 600, gps=False), "image/jpeg") for i in range(3)])
        a, b, c = HousePhoto.objects.all()
        self.client.post(reverse("photo_update", args=[c.pk]), {"action": "up"})
        self.assertEqual([p.pk for p in HousePhoto.objects.all()], [a.pk, c.pk, b.pk])
        self.client.post(reverse("photo_update", args=[b.pk]), {"action": "cover"})
        self.assertEqual(HousePhoto.objects.get(is_cover=True).pk, b.pk)
        self.client.post(reverse("photo_update", args=[a.pk]), {"action": "caption", "caption": "Гостиная"})
        self.assertEqual(HousePhoto.objects.get(pk=a.pk).caption, "Гостиная")
        files = [Path(self.tmp.name) / "media" / b.image, Path(self.tmp.name) / "media" / b.thumb]
        self.client.post(reverse("photo_update", args=[b.pk]), {"action": "delete"})
        self.assertFalse(any(f.exists() for f in files))
        self.assertEqual(HousePhoto.objects.filter(is_cover=True).count(), 1)  # главным стало другое
        page = self.client.get(reverse("house"))
        self.assertContains(page, "Гостиная")
        self.assertContains(page, 'id="photo-list"')


class HouseNameMigrationTests(TestCase):
    def test_auto_names_become_pihtagesh_manual_kept(self):
        mod = importlib.import_module("booking.migrations.0009_house_name_pihtagesh")
        from django.apps import apps

        for old, expected in (("Пихта", "ПихтаГеш"), ("Пихта Гэш", "ПихтаГеш"), ("Гостевой дом", "ПихтаГеш"), ("Мой дом", "Мой дом")):
            House.objects.all().delete()
            House.objects.create(name=old)
            mod.rename(apps, None)
            self.assertEqual(House.get().name, expected, old)


# ---------------------------------------------------------------- дубли и объединение броней

from .merge import build_plan, duplicate_pairs, merge_bookings  # noqa: E402
from .models import LinkedEvent  # noqa: E402


class MergeTests(TestCase):
    """Типичный дубль: бронь загружена из RealtyCalendar, потом пришла с Авито по iCal."""

    def setUp(self):
        self.user = User.objects.create_user("manager", password="very-secret-pass", first_name="Ира")
        self.avito = Channel.objects.create(name="Авито", import_url="https://example.com/a.ics")
        self.guest = G("Пётр", "+79000000007")
        self.excel = Booking.objects.create(
            check_in=d(10), check_out=d(14), channel=self.avito, guest=self.guest, guests=4,
            total_price=40000, comment="Импорт из RealtyCalendar",
        )
        Payment.objects.create(booking=self.excel, amount=12000, method=Payment.Method.PLATFORM)
        # Так дубли уже лежат в базе: бронь по iCal создана, пока авто-привязки не было
        self.ical = Booking.objects.create(
            check_in=d(10), check_out=d(14), channel=self.avito, external_uid="avito-1", external_summary="Reserved"
        )

    def test_duplicate_detected_and_plan_defaults(self):
        self.assertEqual(len(duplicate_pairs()), 1)
        plan = build_plan(self.ical, self.excel)
        self.assertEqual((plan.a, plan.b), (self.excel, self.ical))  # остаётся бронь, внесённая людьми
        self.assertEqual(plan.defaults()["channel"], "b")  # та же площадка — станет бронью площадки
        self.assertEqual(plan.owner_sides(), {"a": "b", "b": "b"})
        self.assertFalse(plan.has_differences)
        self.assertIn("даты и статус дальше берутся с «Авито»", plan.summary())

    def test_merge_into_platform_booking(self):
        result = merge_bookings(self.ical, self.excel, user=self.user)
        self.assertEqual(Booking.objects.count(), 1)
        self.assertEqual(result.pk, self.excel.pk)
        self.assertEqual((result.channel, result.external_uid), (self.avito, "avito-1"))
        self.assertEqual((result.guest, result.guests, result.total_price), (self.guest, 4, 40000))
        self.assertEqual(result.paid, 12000)
        self.assertIn("Импорт из RealtyCalendar", result.comment)
        self.assertIn("объединена с дублем — Авито · по iCal", result.comment)
        self.assertIn("(Ира)", result.comment)
        self.assertEqual(conflict_pairs(), [])
        # Синхронизация дальше ведёт эту бронь: без изменений — ничего не происходит…
        r = apply_feed(self.avito, [FeedEvent("avito-1", d(10), d(14))])
        self.assertEqual((r.created, r.updated, r.cancelled), ([], [], []))
        # …изменили даты на площадке — поменялись и у нас; отменили — отменилась
        apply_feed(self.avito, [FeedEvent("avito-1", d(10), d(15))])
        result.refresh_from_db()
        self.assertEqual(result.check_out, d(15))
        apply_feed(self.avito, [])
        result.refresh_from_db()
        self.assertTrue(result.is_cancelled)
        self.assertEqual(result.paid, 12000)  # платежи и гость остались

    def test_merge_keeps_own_booking_and_links_event(self):
        # Прямая бронь, которую Авито показывает у себя занятой (эхо)
        self.excel.channel = None
        self.excel.save()
        plan = build_plan(self.excel, self.ical)
        self.assertEqual(plan.defaults()["channel"], "a")
        self.assertEqual(plan.owner_sides(), {"a": "", "b": "b"})
        self.assertIn("событие «Авито» будет пропускаться", plan.summary())

        result = merge_bookings(self.excel, self.ical)
        self.assertEqual(Booking.objects.count(), 1)
        self.assertEqual((result.channel, result.external_uid), (None, ""))
        link = LinkedEvent.objects.get()
        self.assertEqual((link.channel, link.uid, link.booking), (self.avito, "avito-1", result))
        # Синхронизация пропускает связанное событие — дубль не возвращается
        r = apply_feed(self.avito, [FeedEvent("avito-1", d(10), d(14))])
        self.assertEqual((r.created, r.updated), ([], []))
        self.assertEqual(Booking.objects.count(), 1)
        # Наша бронь по-прежнему уходит на Авито, иначе даты там откроются
        self.assertIn(f"booking-{result.pk}@", build_export(self.avito).decode())
        # Площадка сменила UID — связь переезжает по датам
        apply_feed(self.avito, [FeedEvent("avito-2", d(10), d(14))])
        self.assertEqual(LinkedEvent.objects.get().uid, "avito-2")
        self.assertEqual(Booking.objects.count(), 1)
        # Событие пропало с площадки — связь удаляется, наша бронь остаётся
        apply_feed(self.avito, [FeedEvent("other", d(40), d(42))])
        self.assertFalse(LinkedEvent.objects.exists())
        result.refresh_from_db()
        self.assertEqual(result.status, Booking.Status.CONFIRMED)

    def test_user_choices_and_payments_from_both(self):
        self.ical.total_price = 45000
        self.ical.comment = "Сумма с площадки"
        self.ical.save()
        Payment.objects.create(booking=self.ical, amount=5000)
        plan = build_plan(self.excel, self.ical)
        self.assertTrue(plan.has_differences)
        self.assertIn("Сумма: 40 000 ₽ или 45 000 ₽ — оставим 40 000 ₽", plan.difference_notes)
        self.assertTrue(plan.warnings)  # платежи у обеих

        result = merge_bookings(self.excel, self.ical, {"total_price": "b", "dates": "a"})
        self.assertEqual(result.total_price, 45000)
        self.assertEqual(result.paid, 17000)
        self.assertIn("Сумма с площадки", result.comment)

    def test_block_with_echo_stays_block(self):
        block = Booking.objects.create(kind=Booking.Kind.BLOCK, check_in=d(20), check_out=d(22), comment="Ремонт")
        echo = Booking.objects.create(check_in=d(20), check_out=d(22), channel=self.avito, external_uid="busy")
        result = merge_bookings(echo, block)
        self.assertEqual((result.kind, result.channel, result.guest), (Booking.Kind.BLOCK, None, None))
        self.assertTrue(LinkedEvent.objects.filter(uid="busy", booking=block).exists())

    def test_import_revert_after_merge(self):
        from .importer import revert_batch

        batch = ImportBatch.objects.create(filename="x.xls", status=ImportBatch.Status.APPLIED)
        Booking.objects.filter(pk=self.excel.pk).update(import_batch=batch)
        merge_bookings(self.excel, self.ical)
        revert_batch(batch)
        self.assertEqual(Booking.objects.count(), 0)
        # Следующая синхронизация вернёт бронь площадки — как до загрузки
        r = apply_feed(self.avito, [FeedEvent("avito-1", d(10), d(14))])
        self.assertEqual(len(r.created), 1)

    def test_pages(self):
        self.client.force_login(self.user)
        cal = self.client.get(reverse("calendar"))
        self.assertContains(cal, "Похоже на дубль")
        self.assertContains(cal, reverse("booking_merge", args=[self.excel.pk, self.ical.pk]))
        card = self.client.get(reverse("booking_edit", args=[self.excel.pk]))
        self.assertContains(card, "похоже на дубль")
        page = self.client.get(reverse("booking_merge", args=[self.ical.pk, self.excel.pk]))
        self.assertContains(page, "Объединить брони")
        self.assertContains(page, "merge-owners")
        resp = self.client.post(reverse("booking_merge", args=[self.ical.pk, self.excel.pk]), {"choice_channel": "b"})
        self.assertRedirects(resp, reverse("booking_edit", args=[self.excel.pk]))
        self.assertEqual(Booking.objects.count(), 1)
        self.assertEqual(self.client.get(reverse("booking_merge", args=[self.excel.pk, self.excel.pk])).status_code, 302)

    def test_bulk_merge_chain(self):
        # Три брони на одни даты (загрузка, ручная и iCal) и ещё один дубль
        Booking.objects.create(check_in=d(10), check_out=d(14), comment="Внесли ещё раз вручную")
        other = Booking.objects.create(check_in=d(30), check_out=d(33), channel=self.avito, guest=G("Анна"))
        Booking.objects.create(check_in=d(30), check_out=d(33), channel=self.avito, external_uid="avito-3")
        self.assertEqual(len(duplicate_pairs()), 4)
        self.client.force_login(self.user)
        page = self.client.get(reverse("duplicates"))
        keys = [r["key"] for r in page.context["rows"]]
        self.client.post(reverse("duplicates"), {"pair": keys})
        self.assertEqual(duplicate_pairs(), [])
        self.assertEqual(Booking.objects.count(), 2)
        other.refresh_from_db()
        self.assertEqual((other.external_uid, other.guest.name), ("avito-3", "Анна"))


class AutoLinkSyncTests(TestCase):
    def setUp(self):
        self.avito = Channel.objects.create(name="Авито", import_url="https://example.com/a.ics")

    def test_manual_platform_booking_is_attached_not_duplicated(self):
        manual = Booking.objects.create(
            check_in=d(5), check_out=d(8), channel=self.avito, guest=G("Олег"), status=Booking.Status.REQUEST
        )
        sent = []
        sync_channel(self.avito, fetch=lambda url: ics(vevent("a1", d(5), d(8))), notify=lambda t: sent.append(t))
        self.assertEqual(Booking.objects.count(), 1)
        manual.refresh_from_db()
        self.assertEqual((manual.external_uid, manual.status), ("a1", Booking.Status.CONFIRMED))
        self.assertIn("уже была внесена", sent[0])
        # Тот же канал с тем же названием (созданный при загрузке) тоже считается этой площадкой
        rc_avito = Channel.objects.create(name="авито ")
        Booking.objects.create(check_in=d(20), check_out=d(22), channel=rc_avito)
        apply_feed(self.avito, [FeedEvent("a1", d(5), d(8)), FeedEvent("a2", d(20), d(22))])
        self.assertEqual(Booking.objects.count(), 2)

    def test_echo_of_exported_booking_is_linked(self):
        own = save_booking(Booking(check_in=d(5), check_out=d(8), guest=G("Свой")))
        # Площадка ещё не забирала наш календарь — это может быть настоящая двойная бронь
        apply_feed(self.avito, [FeedEvent("e1", d(5), d(8))])
        self.assertEqual(len(conflict_pairs()), 1)
        Booking.objects.filter(external_uid="e1").delete()
        # После того как площадка забрала календарь с этой бронью — это эхо
        Channel.objects.filter(pk=self.avito.pk).update(last_exported_at=timezone.now() + timedelta(seconds=1))
        self.avito.refresh_from_db()
        r = apply_feed(self.avito, [FeedEvent("e1", d(5), d(8))])
        self.assertEqual((r.created, r.echoed), ([], [own]))
        self.assertEqual(LinkedEvent.objects.get().booking, own)
        self.assertEqual(conflict_pairs(), [])

    def test_request_is_not_treated_as_echo(self):
        Booking.objects.create(check_in=d(5), check_out=d(8), status=Booking.Status.REQUEST)
        Channel.objects.filter(pk=self.avito.pk).update(last_exported_at=timezone.now() + timedelta(seconds=1))
        self.avito.refresh_from_db()
        r = apply_feed(self.avito, [FeedEvent("e1", d(5), d(8))])
        self.assertEqual(len(r.created), 1)  # заявки на площадки не уходят — это не эхо

    def test_empty_feed_keeps_links(self):
        own = Booking.objects.create(check_in=d(5), check_out=d(8))
        LinkedEvent.objects.create(channel=self.avito, uid="e1", booking=own, check_in=d(5), check_out=d(8))
        apply_feed(self.avito, [FeedEvent("x", d(15), d(18))])
        LinkedEvent.objects.create(channel=self.avito, uid="x2", booking=own, check_in=d(5), check_out=d(8))
        r = apply_feed(self.avito, [])
        self.assertTrue(r.warnings)
        self.assertEqual(LinkedEvent.objects.filter(uid="x2").count(), 1)

    def test_unlink_view(self):
        self.client.force_login(User.objects.create_user("m", password="very-secret-pass"))
        own = Booking.objects.create(check_in=d(5), check_out=d(8))
        link = LinkedEvent.objects.create(channel=self.avito, uid="e1", booking=own, check_in=d(5), check_out=d(8))
        self.assertContains(self.client.get(reverse("booking_edit", args=[own.pk])), "Связано с событием")
        self.client.post(reverse("linked_event_delete", args=[link.pk]))
        self.assertFalse(LinkedEvent.objects.exists())


class StatsComparisonTests(TestCase):
    def test_all_time_preset(self):
        from datetime import date as _date

        today = _date(2026, 11, 15)
        p = resolve_period("all", None, None, today)
        self.assertEqual((p.start, p.last_day, p.previous()), (_date(2023, 1, 1), today, None))
        Booking.objects.create(check_in=_date(2023, 2, 1), check_out=_date(2023, 2, 3), total_price=20000)
        dash = build_dashboard(p, today)
        self.assertEqual(dash.kpi["revenue"], 20000)
        self.assertIsNone(dash.kpi["revenue_delta"])  # сравнивать не с чем
        self.assertEqual(dash.granularity, "month")
        self.assertEqual(dash.series[0]["label"], "янв 23")

    def test_years_and_seasons(self):
        from datetime import date as _date

        from .stats import build_comparison

        today = _date(2026, 11, 15)
        Booking.objects.create(check_in=_date(2024, 12, 30), check_out=_date(2025, 1, 2), total_price=30000)  # 2 ночи 2024, 1 — 2025
        Booking.objects.create(check_in=_date(2025, 7, 1), check_out=_date(2025, 7, 3), total_price=20000)
        Booking.objects.create(check_in=_date(2025, 11, 10), check_out=_date(2025, 11, 12), total_price=10000)
        Booking.objects.create(check_in=_date(2026, 11, 10), check_out=_date(2026, 11, 12), total_price=14000)
        Booking.objects.create(check_in=_date(2026, 12, 1), check_out=_date(2026, 12, 2), total_price=9000)  # будущая
        Booking.objects.create(check_in=_date(2026, 1, 5), check_out=_date(2026, 1, 6), total_price=1,
                               status=Booking.Status.CANCELLED)
        data = build_comparison(today)

        years = {p["key"]: p for p in data["modes"]["year"]["periods"]}
        self.assertEqual(sorted(years), [2023, 2024, 2025, 2026])
        self.assertEqual(years[2024]["total"]["revenue"], 20000)
        self.assertEqual(years[2025]["total"]["revenue"], 10000 + 20000 + 10000)
        self.assertEqual(years[2025]["months"][0]["revenue"], 10000)  # январь 2025
        self.assertEqual(years[2025]["total_delta"]["text"], "+100%")
        self.assertIsNone(years[2026]["total_delta"])  # год ещё идёт
        self.assertTrue(years[2026]["current"])
        self.assertTrue(years[2026]["months"][11]["future"])
        self.assertEqual(years[2026]["months"][11]["revenue"], 9000)
        # «На 15.11»: с начала года по тот же день
        self.assertEqual(years[2026]["to_date"]["revenue"], 14000)
        self.assertEqual(years[2025]["to_date"]["revenue"], 40000)
        self.assertEqual(years[2026]["to_date_delta"]["text"], "−65%")

        seasons = {p["key"]: p for p in data["modes"]["season"]["periods"]}
        self.assertEqual(sorted(seasons), [2022, 2023, 2024, 2025, 2026])
        self.assertEqual(seasons[2026]["label"], "2026/27")
        self.assertTrue(seasons[2022]["partial"])
        self.assertIsNone(seasons[2022]["months"][0])  # ноябрь 2022 — до начала истории
        self.assertEqual(data["modes"]["season"]["months"][0], "ноя")
        s24 = seasons[2024]
        self.assertEqual((s24["winter"]["revenue"], s24["summer"]["revenue"]), (30000, 20000))
        self.assertEqual(seasons[2026]["total"]["revenue"], 14000 + 9000)
        self.assertEqual(seasons[2026]["to_date"]["revenue"], 14000)
        self.assertEqual(seasons[2025]["to_date"]["revenue"], 10000)

    def test_page_has_comparison_and_all_preset(self):
        self.client.force_login(User.objects.create_user("owner", password="very-secret-pass"))
        Booking.objects.create(check_in=d(-400), check_out=d(-397), total_price=30000)
        resp = self.client.get(reverse("stats"), {"period": "all"})
        self.assertContains(resp, "За всё время")
        self.assertContains(resp, "Сравнение лет и сезонов")
        self.assertContains(resp, 'id="compare-data"')
        self.assertNotContains(resp, "сравнение: ")
