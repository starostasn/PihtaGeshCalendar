"""Расчёт статистики по броням за период.

Принципы:
- Выручка распределяется по ночам: бронь на 3 ночи за 30 000 ₽ даёт по 10 000 ₽ на каждую ночь,
  поэтому бронь на стыке месяцев честно делится между ними.
- В выручку и загрузку идут только подтверждённые брони (статус «Бронь»). Заявки и отмены — отдельно.
- Загрузка = занятые ночи / доступные ночи (ночи, закрытые вручную, из доступных вычитаются).
- Заезды, отмены, срок проживания и глубина бронирования считаются по броням с датой заезда в периоде.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db.models import Q
from django.utils import timezone

from .models import Booking, Payment
from .pricing import daterange

MONTHS_SHORT = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

PRESETS = [
    ("this_month", "Этот месяц"),
    ("last_month", "Прошлый месяц"),
    ("next_90", "Следующие 90 дней"),
    ("last_12", "12 месяцев"),
    ("this_year", "Этот год"),
    ("last_year", "Прошлый год"),
    ("all", "За всё время"),
    ("custom", "Свой период"),
]

# С этой даты в календаре есть история броней (загружена из RealtyCalendar).
STATS_START = date(2023, 1, 1)
WINTER_MONTHS = (11, 12, 1, 2, 3, 4)  # горнолыжный сезон в Шерегеше

STAY_BUCKETS = [(1, 1, "1 ночь"), (2, 2, "2"), (3, 3, "3"), (4, 6, "4–6"), (7, 13, "7–13"), (14, 10**6, "14+")]
LEAD_BUCKETS = [
    (0, 2, "0–2 дня"),
    (3, 7, "3–7"),
    (8, 14, "8–14"),
    (15, 30, "15–30"),
    (31, 60, "31–60"),
    (61, 10**6, "60+"),
]


# ---------------------------------------------------------------- период


@dataclass
class Period:
    start: date  # первая ночь
    end: date  # первая ночь ПОСЛЕ периода
    preset: str
    label: str

    @property
    def nights(self) -> int:
        return (self.end - self.start).days

    @property
    def last_day(self) -> date:
        return self.end - timedelta(days=1)

    def previous(self) -> "Period | None":
        """Предыдущий период такой же длины (для сравнения). За всё время сравнивать не с чем."""
        if self.preset == "all":
            return None
        if self.preset in ("this_year", "last_year"):
            start = self.start.replace(year=self.start.year - 1)
            return Period(start, self.start, "prev", "прошлый год")
        if self.preset in ("this_month", "last_month"):
            prev_end = self.start
            prev_start = (self.start - timedelta(days=1)).replace(day=1)
            return Period(prev_start, prev_end, "prev", "прошлый месяц")
        return Period(self.start - timedelta(days=self.nights), self.start, "prev", "предыдущий период")


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, months: int) -> date:
    idx = d.year * 12 + (d.month - 1) + months
    return date(idx // 12, idx % 12 + 1, 1)


def resolve_period(preset: str, date_from: date | None, date_to: date | None, today: date) -> Period:
    if preset == "custom" and date_from and date_to and date_to >= date_from:
        if (date_to - date_from).days > 3 * 366:
            date_to = date_from + timedelta(days=3 * 366)
        return Period(date_from, date_to + timedelta(days=1), "custom", f"{date_from:%d.%m.%Y} – {date_to:%d.%m.%Y}")
    if preset == "last_month":
        start = _add_months(_month_start(today), -1)
        return Period(start, _month_start(today), preset, f"{MONTHS_SHORT[start.month].capitalize()} {start.year}")
    if preset == "next_90":
        return Period(today, today + timedelta(days=90), preset, "Следующие 90 дней")
    if preset == "last_12":
        start = _add_months(_month_start(today), -11)
        return Period(start, _add_months(_month_start(today), 1), preset, "Последние 12 месяцев")
    if preset == "this_year":
        return Period(date(today.year, 1, 1), date(today.year + 1, 1, 1), preset, f"{today.year} год")
    if preset == "last_year":
        return Period(date(today.year - 1, 1, 1), date(today.year, 1, 1), preset, f"{today.year - 1} год")
    if preset == "all":
        start = min(STATS_START, today)
        return Period(start, today + timedelta(days=1), preset, f"За всё время: {start:%d.%m.%Y} – {today:%d.%m.%Y}")
    start = _month_start(today)
    return Period(start, _add_months(start, 1), "this_month", f"{MONTHS_SHORT[start.month].capitalize()} {start.year}")


# ---------------------------------------------------------------- расчёт


def _nights_in(b: Booking, start: date, end: date) -> int:
    return max(0, (min(b.check_out, end) - max(b.check_in, start)).days)


def _share(b: Booking, value: int, start: date, end: date) -> float:
    """Доля суммы брони, приходящаяся на ночи внутри периода."""
    if not value or not b.nights:
        return 0.0
    return value * _nights_in(b, start, end) / b.nights


@dataclass
class Core:
    revenue: float = 0.0
    commission: float = 0.0
    occupied: int = 0
    blocked: int = 0
    nights: int = 0
    arrivals: int = 0
    cancelled: int = 0

    @property
    def available(self) -> int:
        return max(self.nights - self.blocked, 0)

    @property
    def occupancy(self) -> float | None:
        return self.occupied / self.available * 100 if self.available else None

    @property
    def adr(self) -> float | None:
        return self.revenue / self.occupied if self.occupied else None

    @property
    def revpar(self) -> float | None:
        return self.revenue / self.available if self.available else None

    @property
    def cancel_rate(self) -> float | None:
        total = self.arrivals + self.cancelled
        return self.cancelled / total * 100 if total else None


def _load(start: date, end: date):
    qs = (
        Booking.objects.select_related("channel", "guest")
        .prefetch_related("payments")
        .filter(Q(check_in__lt=end, check_out__gt=start) | Q(check_in__gte=start, check_in__lt=end))
    )
    return list(qs)


def _core(bookings: list[Booking], start: date, end: date) -> Core:
    core = Core(nights=(end - start).days)
    occupied, blocked = set(), set()
    for b in bookings:
        if b.status != Booking.Status.CONFIRMED:
            continue
        nights = {d for d in daterange(max(b.check_in, start), min(b.check_out, end))}
        if b.kind == Booking.Kind.BLOCK:
            blocked |= nights
            continue
        occupied |= nights
        core.revenue += _share(b, b.total_price or 0, start, end)
        core.commission += _share(b, b.commission, start, end)
    core.occupied = len(occupied)
    core.blocked = len(blocked - occupied)
    for b in bookings:
        if b.kind == Booking.Kind.BOOKING and start <= b.check_in < end:
            if b.status == Booking.Status.CONFIRMED:
                core.arrivals += 1
            elif b.status == Booking.Status.CANCELLED:
                core.cancelled += 1
    return core


def _delta(cur: float | None, prev: float | None, kind: str = "pct") -> dict | None:
    """Изменение к прошлому периоду: pct — в процентах, pp — в процентных пунктах."""
    if cur is None or prev is None:
        return None
    if kind == "pp":
        diff = cur - prev
        return {"value": diff, "text": f"{diff:+.0f} п.п.".replace("-", "−"), "up": diff > 0, "zero": round(diff) == 0}
    if not prev:
        return None
    diff = (cur - prev) / prev * 100
    return {"value": diff, "text": f"{diff:+.0f}%".replace("-", "−"), "up": diff > 0, "zero": round(diff) == 0}


def _buckets(period: Period) -> tuple[str, list[tuple[date, date, str]]]:
    """Интервалы для графиков: недели для коротких периодов, месяцы — для длинных."""
    out = []
    if period.nights <= 92:
        d = period.start
        while d < period.end:
            week_end = min(d + timedelta(days=7 - d.weekday()), period.end)
            out.append((d, week_end, f"{d:%d.%m}–{week_end - timedelta(days=1):%d.%m}"))
            d = week_end
        return "week", out
    d = period.start
    while d < period.end:
        m_end = min(_add_months(_month_start(d), 1), period.end)
        label = MONTHS_SHORT[d.month]
        if period.start.year != period.last_day.year:
            label += f" {d:%y}"
        out.append((d, m_end, label))
        d = m_end
    return "month", out


@dataclass
class Dashboard:
    period: Period
    prev: Period | None
    kpi: dict = field(default_factory=dict)
    granularity: str = "month"
    series: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    weekdays: list[dict] = field(default_factory=list)
    stay: list[dict] = field(default_factory=list)
    lead: list[dict] = field(default_factory=list)
    debts: list[Booking] = field(default_factory=list)
    requests_count: int = 0
    requests_nights: int = 0
    missing_price: int = 0

    def chart_data(self) -> dict:
        return {
            "granularity": self.granularity,
            "series": self.series,
            "sources": self.sources,
            "weekdays": self.weekdays,
            "stay": self.stay,
            "lead": self.lead,
        }


def build_dashboard(period: Period, today: date) -> Dashboard:
    bookings = _load(period.start, period.end)
    prev = period.previous()
    core = _core(bookings, period.start, period.end)
    prev_bookings = _load(prev.start, prev.end) if prev else []
    pcore = _core(prev_bookings, prev.start, prev.end) if prev else None

    def change(attr: str, kind: str = "pct") -> dict | None:
        return _delta(getattr(core, attr), getattr(pcore, attr), kind) if pcore else None

    dash = Dashboard(period=period, prev=prev)

    confirmed = [b for b in bookings if b.kind == Booking.Kind.BOOKING and b.status == Booking.Status.CONFIRMED]
    arrivals = [b for b in confirmed if period.start <= b.check_in < period.end]
    overlapping = [b for b in confirmed if _nights_in(b, period.start, period.end) > 0]

    # ---- оплаты
    payments = Payment.objects.filter(date__gte=period.start, date__lt=period.end)
    received = sum(p.signed_amount for p in payments)
    debts = [b for b in overlapping if b.balance_due and b.balance_due > 0]
    dash.debts = sorted(debts, key=lambda b: b.check_in)
    outstanding = sum(b.balance_due for b in debts)

    stays = [b.nights for b in arrivals]
    leads = [max((b.check_in - timezone.localtime(b.created_at).date()).days, 0) for b in arrivals]
    prev_arrivals = [
        b
        for b in prev_bookings
        if b.kind == Booking.Kind.BOOKING and b.status == Booking.Status.CONFIRMED and prev.start <= b.check_in < prev.end
    ]

    dash.kpi = {
        "revenue": round(core.revenue),
        "revenue_delta": change("revenue"),
        "net": round(core.revenue - core.commission),
        "commission": round(core.commission),
        "occupancy": core.occupancy,
        "occupancy_delta": change("occupancy", "pp"),
        "occupied": core.occupied,
        "available": core.available,
        "blocked": core.blocked,
        "adr": core.adr,
        "adr_delta": change("adr"),
        "revpar": core.revpar,
        "arrivals": core.arrivals,
        "arrivals_delta": _delta(core.arrivals, len(prev_arrivals)) if prev_arrivals else None,
        "avg_stay": sum(stays) / len(stays) if stays else None,
        "avg_lead": sum(leads) / len(leads) if leads else None,
        "cancelled": core.cancelled,
        "cancel_rate": core.cancel_rate,
        "received": received,
        "outstanding": outstanding,
    }

    reqs = [b for b in bookings if b.status == Booking.Status.REQUEST and _nights_in(b, period.start, period.end)]
    dash.requests_count = len(reqs)
    dash.requests_nights = sum(_nights_in(b, period.start, period.end) for b in reqs)
    dash.missing_price = sum(1 for b in overlapping if b.total_price is None)

    # ---- по неделям / месяцам
    dash.granularity, buckets = _buckets(period)
    for b_start, b_end, label in buckets:
        c = _core(bookings, b_start, b_end)
        dash.series.append(
            {
                "label": label,
                "start": b_start.isoformat(),
                "revenue": round(c.revenue),
                "occupancy": round(c.occupancy, 1) if c.occupancy is not None else None,
                "occupied": c.occupied,
                "available": c.available,
                "future": b_start >= today,
            }
        )

    # ---- источники
    by_source: dict[int | None, dict] = {}
    for b in overlapping:
        key = b.channel_id
        row = by_source.setdefault(
            key,
            {
                "name": b.source_name,
                "color": b.channel.color if b.channel else "#2E7D32",
                "commission_percent": float(b.channel.commission_percent) if b.channel else 0.0,
                "bookings": 0,
                "nights": 0,
                "revenue": 0.0,
                "commission": 0.0,
            },
        )
        row["bookings"] += 1
        row["nights"] += _nights_in(b, period.start, period.end)
        row["revenue"] += _share(b, b.total_price or 0, period.start, period.end)
        row["commission"] += _share(b, b.commission, period.start, period.end)
    total_rev = sum(r["revenue"] for r in by_source.values()) or 1
    for r in by_source.values():
        r["revenue"] = round(r["revenue"])
        r["commission"] = round(r["commission"])
        r["net"] = r["revenue"] - r["commission"]
        r["share"] = round(r["revenue"] * 100 / total_rev, 1)
    dash.sources = sorted(by_source.values(), key=lambda r: (-r["revenue"], -r["nights"]))

    # ---- дни недели (по ночам)
    occ = [0] * 7
    avail = [0] * 7
    occupied_nights, blocked_nights = set(), set()
    for b in bookings:
        if b.status != Booking.Status.CONFIRMED:
            continue
        nights = set(daterange(max(b.check_in, period.start), min(b.check_out, period.end)))
        (blocked_nights if b.kind == Booking.Kind.BLOCK else occupied_nights).update(nights)
    for d in daterange(period.start, period.end):
        if d in blocked_nights and d not in occupied_nights:
            continue
        avail[d.weekday()] += 1
        if d in occupied_nights:
            occ[d.weekday()] += 1
    dash.weekdays = [
        {"label": WEEKDAYS[i], "occupancy": round(occ[i] * 100 / avail[i], 1) if avail[i] else None, "nights": occ[i]}
        for i in range(7)
    ]

    # ---- распределения
    def distribution(values, buckets_spec):
        return [{"label": label, "count": sum(1 for v in values if lo <= v <= hi)} for lo, hi, label in buckets_spec]

    dash.stay = distribution(stays, STAY_BUCKETS)
    dash.lead = distribution(leads, LEAD_BUCKETS)
    return dash


# ---------------------------------------------------------------- сравнение лет и сезонов

MONTHS_FULL = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
               "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
MAX_COMPARED = 8  # больше линий на одном графике уже не различить


def _shift_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 февраля
        return d.replace(year=d.year + years, day=28)


def _totals(bookings: list[Booking], start: date, end: date) -> dict:
    c = _core(bookings, start, end)
    return {
        "revenue": round(c.revenue),
        "occupied": c.occupied,
        "available": c.available,
        "occupancy": round(c.occupancy, 1) if c.occupancy is not None else None,
        "adr": round(c.adr) if c.adr is not None else None,
        "arrivals": c.arrivals,
    }


def _season_key(d: date) -> int:
    """Сезон 2025/26 — с 1 ноября 2025 по 31 октября 2026: зима (ноябрь–апрель), потом лето."""
    return d.year if d.month >= 11 else d.year - 1


def _bounds(mode: str, key: int) -> tuple[date, date, str]:
    if mode == "year":
        return date(key, 1, 1), date(key + 1, 1, 1), str(key)
    return date(key, 11, 1), date(key + 1, 11, 1), f"{key}/{(key + 1) % 100:02d}"


def build_comparison(today: date) -> dict:
    """Помесячные показатели по годам и по сезонам — для графика «Сравнение лет».

    Для текущего года (сезона) будущие месяцы считаются по уже сделанным броням.
    «На эту дату» — показатели с начала каждого года по тот же день, что сегодня:
    честное сравнение незаконченного года с прошлыми.
    """
    data_start = min(STATS_START, today)
    bookings = _load(date(data_start.year - 1, 11, 1), date(today.year + 2, 1, 1))

    result = {"today": today.isoformat(), "modes": {}}
    for mode in ("year", "season"):
        key_of = (lambda d: d.year) if mode == "year" else _season_key
        cur_key = key_of(today)
        keys = list(range(key_of(data_start), cur_key + 1))[-MAX_COMPARED:]
        first_month = 1 if mode == "year" else 11
        months = [MONTHS_SHORT[(first_month - 1 + i) % 12 + 1] for i in range(12)]
        periods = []
        for key in keys:
            start, end, label = _bounds(mode, key)
            from_ = max(start, data_start)
            row = {
                "key": key,
                "label": label,
                "current": key == cur_key,
                "partial": start < data_start,
                "finished": end <= today,
                "months": [],
            }
            for i in range(12):
                m_start, m_end = _add_months(start, i), _add_months(start, i + 1)
                if m_end <= data_start:
                    row["months"].append(None)  # до начала истории броней
                    continue
                item = _totals(bookings, max(m_start, data_start), m_end)
                item["future"] = m_end > today
                row["months"].append(item)
            row["total"] = _totals(bookings, from_, end)
            if mode == "season":
                middle = _add_months(start, 6)
                row["winter"] = _totals(bookings, from_, middle) if middle > data_start else None
                row["summer"] = _totals(bookings, max(middle, data_start), end)
            same_day = _shift_years(today, key - cur_key)
            row["until"] = same_day.isoformat()
            row["to_date"] = _totals(bookings, from_, min(same_day, end)) if same_day > from_ else None
            periods.append(row)

        for prev, row in zip([None] + periods, periods):
            comparable = prev is not None and not prev["partial"]
            row["total_delta"] = (
                _delta(row["total"]["revenue"], prev["total"]["revenue"])
                if comparable and row["finished"] and prev["finished"]
                else None
            )
            row["to_date_delta"] = (
                _delta(row["to_date"]["revenue"], prev["to_date"]["revenue"])
                if comparable and row["to_date"] and prev["to_date"]
                else None
            )
        result["modes"][mode] = {"months": months, "periods": periods}
    return result
