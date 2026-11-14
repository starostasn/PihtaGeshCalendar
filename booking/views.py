import calendar as pycalendar
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from .forms import BookingForm, ChannelForm, HouseForm, PaymentForm, PeriodPriceForm, PriceRuleForm
from .ical import build_export, sync_channel
from .guests import guest_choice_payload
from .models import Booking, Channel, Guest, House, Payment, PriceRule, SyncLog
from .permissions import admin_required, forbidden, is_admin
from .pricing import PriceCalculator
from .services import OverlapError, conflict_pairs, is_possible_echo, save_booking
from .stats import PRESETS, build_dashboard, resolve_period

MONTH_NAMES = [
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
]


# ---------------------------------------------------------------- календарь


@dataclass
class Day:
    date: date
    in_month: bool
    is_today: bool
    is_past: bool
    price: int
    bookings: list[Booking] = field(default_factory=list)  # занимают ночь с этой даты
    ending: Booking | None = None  # бронь, у которой в этот день выезд

    @property
    def booking(self) -> Booking | None:
        return self.bookings[0] if self.bookings else None

    @property
    def conflict(self) -> bool:
        return len(self.bookings) > 1

    @property
    def is_start(self) -> bool:
        return bool(self.booking and self.booking.check_in == self.date)

    @property
    def show_label(self) -> bool:
        return bool(self.booking and (self.is_start or self.date.weekday() == 0))

    @property
    def label_span(self) -> int:
        """Сколько ячеек до конца брони или до конца недели занимает подпись."""
        if not self.booking:
            return 0
        week_end = self.date + timedelta(days=7 - self.date.weekday())
        return (min(self.booking.check_out, week_end) - self.date).days


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _build_month(year: int, month: int, bookings: list[Booking], calc: PriceCalculator, today: date):
    weeks = []
    for week in pycalendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        row = []
        for d in week:
            day = Day(
                date=d,
                in_month=d.month == month,
                is_today=d == today,
                is_past=d < today,
                price=calc.night_price(d).price,
            )
            for b in bookings:
                if b.check_in <= d < b.check_out:
                    day.bookings.append(b)
                elif b.check_out == d:
                    day.ending = b
            row.append(day)
        weeks.append(row)
    season = month in (11, 12, 1, 2, 3, 4)  # горнолыжный сезон в Шерегеше — примерно с ноября по апрель
    return {"year": year, "month": month, "title": f"{MONTH_NAMES[month]} {year}", "weeks": weeks, "season": season}


@login_required
def calendar_view(request):
    today = timezone.localdate()
    try:
        y, m = (int(x) for x in request.GET.get("m", "").split("-"))
        date(y, m, 1)
    except (ValueError, TypeError):
        y, m = today.year, today.month

    months_count = 2
    first = date(y, m, 1) - timedelta(days=7)
    ly, lm = _add_months(y, m, months_count)
    last = date(ly, lm, 1) + timedelta(days=7)
    bookings = list(
        Booking.objects.active()
        .overlapping(first, last)
        .select_related("channel", "guest")
        .order_by("check_in", "pk")
    )
    calc = PriceCalculator()
    months = [_build_month(*_add_months(y, m, i), bookings, calc, today) for i in range(months_count)]

    upcoming = (
        Booking.objects.active()
        .filter(check_out__gte=today)
        .select_related("channel", "guest")
        .prefetch_related("payments")
        .order_by("check_in")[:20]
    )
    conflicts = [(a, b, is_possible_echo(a, b)) for a, b in conflict_pairs()]
    failing = Channel.objects.filter(is_active=True, last_sync_ok=False)

    return render(
        request,
        "booking/calendar.html",
        {
            "months": months,
            "prev_m": "%04d-%02d" % _add_months(y, m, -1),
            "next_m": "%04d-%02d" % _add_months(y, m, 1),
            "weekday_names": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
            "upcoming": upcoming,
            "conflicts": conflicts,
            "failing_channels": failing,
            "today": today,
            "channels": Channel.objects.all(),
        },
    )


# ---------------------------------------------------------------- брони


def _booking_form_view(request, booking: Booking | None, payment_form: PaymentForm | None = None):
    house = House.get()
    is_new = booking is None
    if request.method == "POST" and payment_form is None:
        form = BookingForm(request.POST, instance=booking)
        if form.is_valid():
            obj = form.save(commit=False)
            if is_new:
                obj.created_by = request.user
            try:
                with transaction.atomic():
                    if form.new_guest is not None:
                        form.new_guest.created_by = request.user
                        form.new_guest.save()
                        obj.guest = form.new_guest
                    save_booking(obj)
            except OverlapError as exc:
                for c in exc.conflicts:
                    form.add_error(
                        None,
                        f"Даты пересекаются: {c.source_name}, {c.title}, "
                        f"{c.check_in:%d.%m}–{c.check_out:%d.%m.%Y}",
                    )
            else:
                if is_new:
                    form.save_initial_payment(obj, request.user)
                if form.matched_guest is not None:
                    messages.info(request, f"Телефон уже был в справочнике — бронь привязана к гостю «{form.matched_guest.name}»")
                elif form.new_guest is not None:
                    messages.info(request, f"Гость «{form.new_guest.name}» добавлен в справочник")
                if obj.kind == Booking.Kind.BLOCK:
                    text = "Даты открыты" if obj.is_cancelled else "Даты закрыты"
                else:
                    text = f"Сохранено: {obj.get_status_display().lower()}"
                messages.success(request, text)
                return redirect(f"{reverse('calendar')}?m={obj.check_in:%Y-%m}")
    else:
        initial = {}
        if is_new:
            check_in = parse_date(request.GET.get("check_in", "") or "")
            check_out = parse_date(request.GET.get("check_out", "") or "")
            if check_in and not check_out:
                check_out = check_in + timedelta(days=1)
            if check_in and check_out and check_out > check_in:
                initial.update(check_in=check_in, check_out=check_out)
                initial["total_price"] = PriceCalculator(house).quote(check_in, check_out).total
            if request.GET.get("kind") == Booking.Kind.BLOCK:
                initial["kind"] = Booking.Kind.BLOCK
            guest_id = request.GET.get("guest", "")
            if guest_id.isdigit() and Guest.objects.filter(pk=int(guest_id)).exists():
                initial["guest"] = int(guest_id)
        form = BookingForm(instance=booking, initial=initial)

    selected_guest = None
    guest_value = form["guest"].value()
    if guest_value and str(guest_value).isdigit():
        selected_guest = Guest.objects.filter(pk=int(guest_value)).first()
    if booking is not None and payment_form is None:
        balance = booking.balance_due
        payment_form = PaymentForm(initial={"amount": balance if balance and balance > 0 else None})
    return render(
        request,
        "booking/booking_form.html",
        {
            "form": form,
            "booking": booking,
            "is_new": is_new,
            "house": house,
            "payment_form": payment_form,
            "payments": list(booking.payments.all()) if booking else [],
            "selected_guest": guest_choice_payload(selected_guest),
        },
    )


@login_required
def booking_create(request):
    return _booking_form_view(request, None)


@login_required
def booking_edit(request, pk: int):
    return _booking_form_view(request, get_object_or_404(Booking.objects.select_related("channel", "guest"), pk=pk))


@login_required
@require_POST
def payment_add(request, pk: int):
    booking = get_object_or_404(Booking.objects.select_related("channel"), pk=pk)
    form = PaymentForm(request.POST)
    if not form.is_valid():
        return _booking_form_view(request, booking, payment_form=form)
    payment = form.save(commit=False)
    payment.booking = booking
    payment.created_by = request.user
    payment.save()
    messages.success(request, f"{payment.get_kind_display()} {payment.amount:,} ₽ добавлена".replace(",", "\u00a0"))
    return redirect(f"{reverse('booking_edit', args=[pk])}#payments")


@admin_required
@require_POST
def payment_delete(request, pk: int):
    payment = get_object_or_404(Payment, pk=pk)
    booking_pk = payment.booking_id
    payment.delete()
    messages.success(request, "Платёж удалён")
    return redirect(f"{reverse('booking_edit', args=[booking_pk])}#payments")


@login_required
def quote_api(request):
    check_in = parse_date(request.GET.get("check_in", "") or "")
    check_out = parse_date(request.GET.get("check_out", "") or "")
    if not check_in or not check_out or check_out <= check_in or (check_out - check_in).days > 366:
        return JsonResponse({"error": "Неверные даты"}, status=400)
    q = PriceCalculator().quote(check_in, check_out)
    exclude = request.GET.get("exclude")
    overlaps = Booking.objects.active().overlapping(check_in, check_out)
    if exclude and exclude.isdigit():
        overlaps = overlaps.exclude(pk=int(exclude))
    return JsonResponse(
        {
            "total": q.total,
            "nights": q.nights_count,
            "min_stay": q.min_stay,
            "min_stay_ok": q.min_stay_ok,
            "available": not overlaps.exists(),
            "prices": [{"date": n.night.isoformat(), "price": n.price} for n in q.nights],
        }
    )


@login_required
def booking_list(request):
    show = request.GET.get("show", "upcoming")
    today = timezone.localdate()
    qs = Booking.objects.select_related("channel", "guest").prefetch_related("payments")
    if show == "requests":
        qs = qs.filter(status=Booking.Status.REQUEST, check_out__gte=today).order_by("check_in")
    elif show == "debts":
        qs = [
            b
            for b in qs.confirmed().filter(kind=Booking.Kind.BOOKING).order_by("check_in")
            if b.balance_due and b.balance_due > 0
        ]
    elif show == "cancelled":
        qs = qs.filter(status=Booking.Status.CANCELLED).order_by("-updated_at")
    elif show == "past":
        qs = qs.active().filter(check_out__lt=today).order_by("-check_in")
    else:
        show = "upcoming"
        qs = qs.active().filter(check_out__gte=today).order_by("check_in")
    return render(request, "booking/booking_list.html", {"bookings": qs[:200], "show": show})


# ---------------------------------------------------------------- цены


@login_required
def prices_view(request):
    house = House.get()
    house_form = HouseForm(instance=house, prefix="house")
    period_form = PeriodPriceForm(prefix="period")
    if request.method == "POST":
        if not is_admin(request.user):
            return forbidden(request, "Цены меняет администратор.")
        if "save_house" in request.POST:
            house_form = HouseForm(request.POST, instance=house, prefix="house")
            if house_form.is_valid():
                house_form.save()
                messages.success(request, "Настройки сохранены")
                return redirect("prices")
        elif "save_period" in request.POST:
            period_form = PeriodPriceForm(request.POST, prefix="period")
            if period_form.is_valid():
                rule = period_form.create_rule()
                messages.success(request, f"Добавлено правило «{rule.name}»")
                return redirect("prices")

    rules = PriceRule.objects.all()
    today = timezone.localdate()
    calc = PriceCalculator(house)
    preview = [calc.night_price(today + timedelta(days=i)) for i in range(14)]
    return render(
        request,
        "booking/prices.html",
        {
            "house_form": house_form,
            "period_form": period_form,
            "rules": rules,
            "preview": preview,
            "today": today,
        },
    )


@admin_required
def rule_edit(request, pk: int | None = None):
    rule = get_object_or_404(PriceRule, pk=pk) if pk else None
    if request.method == "POST":
        form = PriceRuleForm(request.POST, instance=rule)
        if form.is_valid():
            form.save()
            messages.success(request, "Правило сохранено")
            return redirect("prices")
    else:
        initial = {}
        if rule is None and request.GET.get("preset") == "weekend":
            initial = {"name": "Выходные", "weekdays": ["4", "5"], "priority": PriceRule.PRIORITY_WEEKDAYS}
        form = PriceRuleForm(instance=rule, initial=initial)
    return render(request, "booking/rule_form.html", {"form": form, "rule": rule})


@admin_required
@require_POST
def rule_delete(request, pk: int):
    get_object_or_404(PriceRule, pk=pk).delete()
    messages.success(request, "Правило удалено")
    return redirect("prices")


# ---------------------------------------------------------------- площадки


def export_url(channel: Channel) -> str:
    return settings.PUBLIC_BASE_URL.rstrip("/") + reverse("ical_export", args=[channel.export_token])


@admin_required
def channels_view(request):
    channels = list(Channel.objects.all())
    for ch in channels:
        ch.export_url = export_url(ch)
    logs = SyncLog.objects.select_related("channel")[:30]
    local_mode = urlsplit(settings.PUBLIC_BASE_URL).hostname in {"127.0.0.1", "localhost"}
    return render(
        request, "booking/channels.html", {"channels": channels, "logs": logs, "local_mode": local_mode}
    )


@admin_required
def channel_edit(request, pk: int | None = None):
    channel = get_object_or_404(Channel, pk=pk) if pk else None
    if request.method == "POST":
        form = ChannelForm(request.POST, instance=channel)
        if form.is_valid():
            channel = form.save()
            messages.success(request, "Площадка сохранена")
            return redirect("channels")
    else:
        form = ChannelForm(instance=channel, initial={} if channel else {"name": "Авито", "color": "#00AAFF"})
    return render(
        request,
        "booking/channel_form.html",
        {"form": form, "channel": channel, "export_url": export_url(channel) if channel else ""},
    )


@admin_required
@require_POST
def channel_sync(request, pk: int):
    channel = get_object_or_404(Channel, pk=pk)
    if not channel.import_url:
        messages.error(request, "Сначала укажите ссылку на календарь площадки")
        return redirect("channels")
    result = sync_channel(channel)
    if result.ok:
        messages.success(
            request,
            f"«{channel.name}»: новых {len(result.created)}, изменено {len(result.updated)}, "
            f"отменено {len(result.cancelled)}",
        )
        for w in result.warnings:
            messages.warning(request, w)
    else:
        messages.error(request, f"«{channel.name}»: {result.error}")
    return redirect("channels")


@admin_required
@require_POST
def channel_new_token(request, pk: int):
    from .models import new_token

    channel = get_object_or_404(Channel, pk=pk)
    channel.export_token = new_token()
    channel.save(update_fields=["export_token"])
    messages.warning(request, "Ссылка на наш календарь изменилась — обновите её на площадке!")
    return redirect("channel_edit", pk=pk)


def ical_export(request, token: str):
    """Публичная (по секретной ссылке) выгрузка занятых дат для площадки."""
    channel = Channel.objects.filter(export_token=token).first()
    if channel is None:
        raise Http404()
    Channel.objects.filter(pk=channel.pk).update(last_exported_at=timezone.now())
    resp = HttpResponse(build_export(channel), content_type="text/calendar; charset=utf-8")
    resp["Content-Disposition"] = 'inline; filename="calendar.ics"'
    resp["Cache-Control"] = "no-cache"
    return resp


# ---------------------------------------------------------------- статистика


@login_required
def stats_view(request):
    today = timezone.localdate()
    preset = request.GET.get("period", "this_month")
    date_from = parse_date(request.GET.get("from", "") or "")
    date_to = parse_date(request.GET.get("to", "") or "")
    period = resolve_period(preset, date_from, date_to, today)
    dash = build_dashboard(period, today)
    return render(
        request,
        "booking/stats.html",
        {
            "dash": dash,
            "kpi": dash.kpi,
            "presets": PRESETS,
            "preset": period.preset,
            "date_from": period.start,
            "date_to": period.last_day,
            "chart_data": dash.chart_data(),
            "sources_height": max(120, 40 * len(dash.sources) + 40),
        },
    )
