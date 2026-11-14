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
        self.user = User.objects.create_superuser("owner", password="very-secret-pass")

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
            {"check_in": d(1).isoformat(), "check_out": d(20).isoformat(), "guest_name": "Олег", "status": "cancelled"},
        )
        self.assertEqual(resp.status_code, 302)
        b.refresh_from_db()
        self.assertEqual((b.check_in, b.check_out, b.guest_name, b.channel), (d(6), d(8), "Олег", ch))
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
        save_booking(Booking(check_in=d(10), check_out=d(12), status=Booking.Status.REQUEST, guest_name="Заявка"))
        save_booking(Booking(check_in=d(20), check_out=d(22), guest_name="Бронь"))
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
        b = Booking.objects.create(check_in=d(1), check_out=d(3), total_price=20000, guest_name="Должник")
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
        b = Booking.objects.create(check_in=d(-3), check_out=d(2), total_price=50000, guest_name="Гость")
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
