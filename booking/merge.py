"""Объединение дублей броней.

Дубль обычно появляется так: бронь уже есть у нас (внесена вручную или загружена
из RealtyCalendar), а потом та же бронь приходит из календаря площадки по iCal.
Объединение оставляет одну бронь и собирает в ней данные обеих: гостя, сумму,
число гостей, комментарии и все платежи.

Как итоговая бронь связана с площадкой, зависит от выбранного источника:
- источник — площадка, приславшая бронь по iCal: бронь становится «бронью площадки»,
  её даты и статус дальше меняет синхронизация (отменили на Авито — отменится и у нас);
- источник другой (например, «Напрямую»): бронь остаётся вашей, а событие площадки
  запоминается как связанное (LinkedEvent) — синхронизация его пропускает,
  и дубль не появляется снова.
"""

from dataclasses import dataclass, field

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .models import Booking, Channel, LinkedEvent
from .services import conflict_pairs

SIDES = ("a", "b")


def same_platform(x: Channel | None, y: Channel | None) -> bool:
    """Одна и та же площадка: тот же канал или канал с тем же названием
    (например, «Авито», созданный при загрузке из RealtyCalendar, и «Авито» с iCal)."""
    if x is None or y is None:
        return False
    return x.pk == y.pk or x.name.strip().lower() == y.name.strip().lower()


def order_pair(x: Booking, y: Booking) -> tuple[Booking, Booking]:
    """Какая запись останется (a), а какая будет удалена (b). Остаётся бронь,
    внесённая людьми (у неё автор, дата создания, связь с загрузкой); из двух таких — более ранняя."""
    if x.is_imported != y.is_imported:
        return (y, x) if x.is_imported else (x, y)
    return (x, y) if x.pk < y.pk else (y, x)


def _dates(b: Booking) -> str:
    return f"{b.check_in:%d.%m}–{b.check_out:%d.%m.%Y} ({b.nights} н.)"


def _rub(v: int | None) -> str:
    return "—" if v is None else f"{v:,} ₽".replace(",", " ")


def _guest(b: Booking) -> str:
    if not b.guest_id:
        return "—"
    return f"{b.guest.name}, {b.guest.phone_display}" if b.guest.phone else b.guest.name


def _source(b: Booking) -> str:
    return b.source_name + (" · по iCal" if b.is_imported else "")


@dataclass
class FieldChoice:
    name: str
    label: str
    values: dict  # {"a": текст, "b": текст}
    default: str = "a"
    differs: bool = False  # оба значения заполнены и не совпадают — стоит посмотреть
    choosable: bool = False  # показывать выбор: оба значения есть и они разные
    note: str = ""  # пояснение, если выбирать нечего

    @property
    def same(self) -> bool:
        return self.values["a"] == self.values["b"]

    @property
    def value(self) -> str:
        return self.values[self.default]


@dataclass
class MergePlan:
    a: Booking  # останется
    b: Booking  # будет удалена
    fields: list[FieldChoice] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def side(self, key: str) -> Booking:
        return self.a if key == "a" else self.b

    def defaults(self) -> dict[str, str]:
        return {f.name: f.default for f in self.fields}

    def owner_for(self, channel_side: str) -> Booking | None:
        """Импортированная бронь, чья площадка станет источником итоговой брони (или None)."""
        chosen = self.side(channel_side)
        if chosen.is_imported:
            return chosen
        for s in (self.a, self.b):
            if s.is_imported and same_platform(s.channel, chosen.channel):
                return s
        return None

    def owner_sides(self) -> dict[str, str]:
        """Для каждого варианта источника — чья бронь с iCal станет основой ("a"/"b"), или "" —
        бронь останется вашей."""
        out = {}
        for k in SIDES:
            owner = self.owner_for(k)
            out[k] = "" if owner is None else ("a" if owner is self.a else "b")
        return out

    @property
    def has_imported(self) -> bool:
        return self.a.is_imported or self.b.is_imported

    @property
    def difference_notes(self) -> list[str]:
        notes = []
        for f in self.fields:
            if f.differs:
                other = "b" if f.default == "a" else "a"
                notes.append(f"{f.label}: {f.values[f.default]} или {f.values[other]} — оставим {f.values[f.default]}")
        return notes

    def summary(self, choices: dict[str, str] | None = None) -> str:
        """Короткое описание итога — для списка дублей."""
        c = {**self.defaults(), **(choices or {})}
        owner = self.owner_for(c["channel"])
        pick = lambda name: self.side(c[name])  # noqa: E731
        parts = [owner.channel.name if owner else pick("channel").source_name]
        if pick("guest").guest_id:
            parts.append(pick("guest").guest.name)
        if pick("total_price").total_price is not None:
            parts.append(_rub(pick("total_price").total_price))
        text = " · ".join(parts)
        others = [s for s in (self.a, self.b) if s.is_imported and s is not owner]
        if owner:
            text += f" — даты и статус дальше берутся с «{owner.channel.name}»"
        if others:
            names = ", ".join(f"«{s.channel.name}»" for s in others)
            text += f" — событие {names} будет пропускаться, дубль не вернётся"
        return text

    @property
    def has_differences(self) -> bool:
        return any(f.differs for f in self.fields)


def build_plan(x: Booking, y: Booking) -> MergePlan:
    a, b = order_pair(x, y)
    plan = MergePlan(a=a, b=b)

    def add(name, label, getter, display, empty=lambda v: v is None):
        va, vb = getter(a), getter(b)
        fc = FieldChoice(name=name, label=label, values={"a": display(a), "b": display(b)})
        if empty(va) and not empty(vb):
            fc.default = "b"
        fc.differs = not empty(va) and not empty(vb) and va != vb
        fc.choosable = fc.differs
        if not fc.choosable and not fc.same and (empty(va) or empty(vb)):
            fc.note = f"из брони {1 if fc.default == 'a' else 2}"
        plan.fields.append(fc)
        return fc

    add("kind", "Тип", lambda s: s.kind, lambda s: s.get_kind_display(), empty=lambda v: False)
    st = add("status", "Статус", lambda s: s.status, lambda s: s.get_status_display(), empty=lambda v: False)
    if a.is_cancelled and not b.is_cancelled:
        st.default = "b"
    add("dates", "Даты", lambda s: (s.check_in, s.check_out), _dates, empty=lambda v: False)
    ch = add("channel", "Источник", lambda s: s.channel_id, _source, empty=lambda v: False)
    ch.differs = False  # что значит выбор источника, видно в итоге объединения
    if same_platform(a.channel, b.channel):
        # Одна площадка (даже если это два канала с одинаковым названием) — выбирать нечего.
        ch.choosable = False
        if b.is_imported and not a.is_imported:
            ch.default = "b"
    add("guest", "Гость", lambda s: s.guest_id, _guest)
    add("guests", "Гостей", lambda s: s.guests, lambda s: "—" if s.guests is None else str(s.guests))
    add("total_price", "Сумма", lambda s: s.total_price, lambda s: _rub(s.total_price))

    # У брони с iCal тип всегда «Гости» и статус «Бронь» — это не данные человека, их не предлагаем.
    if a.is_imported != b.is_imported:
        human = "b" if a.is_imported else "a"
        for fc in plan.fields:
            if fc.name in ("kind", "status"):
                fc.differs = fc.choosable = False
                fc.default = human
                fc.note = ""

    if a.payments.exists() and b.payments.exists():
        plan.warnings.append(
            "Платежи есть у обеих броней — после объединения проверьте, не задвоились ли они."
        )
    if not (a.check_in < b.check_out and b.check_in < a.check_out):
        plan.warnings.append("Даты броней не пересекаются — похоже, это разные брони.")
    return plan


class MergeError(Exception):
    pass


@transaction.atomic
def merge_bookings(x: Booking, y: Booking, choices: dict[str, str] | None = None, user=None) -> Booking:
    """Объединяет две брони в одну. choices: {поле: "a" | "b"} для полей плана (a — остающаяся)."""
    x = Booking.objects.select_for_update().get(pk=x.pk)
    y = Booking.objects.select_for_update().get(pk=y.pk)
    if x.pk == y.pk:
        raise MergeError("Нельзя объединить бронь саму с собой")
    plan = build_plan(x, y)
    a, b = plan.a, plan.b
    c = plan.defaults()
    for k, v in (choices or {}).items():
        if k in c and v in SIDES:
            c[k] = v
    pick = plan.side

    owner = plan.owner_for(c["channel"])
    links = [s for s in (a, b) if s.is_imported and s is not owner]
    link_data = [
        {"channel": s.channel, "uid": s.external_uid, "check_in": s.check_in, "check_out": s.check_out,
         "summary": s.external_summary}
        for s in links
        if s.channel is not None
    ]

    dates_from = owner or pick(c["dates"])
    status_from = owner or pick(c["status"])
    result = {
        "kind": pick(c["kind"]).kind,
        "status": status_from.status,
        "check_in": dates_from.check_in,
        "check_out": dates_from.check_out,
        "channel": owner.channel if owner else pick(c["channel"]).channel,
        "external_uid": owner.external_uid if owner else "",
        "external_summary": owner.external_summary if owner else (a.external_summary if not a.is_imported else ""),
        "guest": pick(c["guest"]).guest,
        "guests": pick(c["guests"]).guests,
        "total_price": pick(c["total_price"]).total_price,
        "cancel_reason": status_from.cancel_reason if status_from.is_cancelled else "",
    }
    if result["kind"] == Booking.Kind.BLOCK:
        result["guest"] = None
        if result["status"] == Booking.Status.REQUEST:
            result["status"] = Booking.Status.CONFIRMED

    comments = []
    for s in (a, b):
        text = s.comment.strip()
        if text and text not in comments:
            comments.append(text)
    who = ""
    if user is not None:
        who = f" ({user.get_full_name() or user.username})"
    comments.append(
        f"{timezone.localdate():%d.%m.%Y}: объединена с дублем — {_source(b)}, {_dates(b)}{who}"
    )

    # Сначала переносим всё, что ссылается на удаляемую бронь, потом удаляем её,
    # и только затем пишем итог (иначе мешает уникальность UID площадки).
    b.payments.update(booking=a)
    LinkedEvent.objects.filter(booking=b).update(booking=a)
    if not a.import_batch_id and b.import_batch_id:
        a.import_batch_id = b.import_batch_id
    if not a.created_by_id and b.created_by_id:
        a.created_by_id = b.created_by_id
    # Связь с бронью в API Авито (номер, статус, суммы) переходит в итоговую бронь
    avito_fields = {}
    if a.avito_booking_id is None and b.avito_booking_id is not None:
        avito_fields = {
            "avito_booking_id": b.avito_booking_id, "avito_status": b.avito_status,
            "avito_data": b.avito_data, "commission_amount": b.commission_amount,
        }
    elif a.commission_amount is None and b.commission_amount is not None:
        avito_fields = {"commission_amount": b.commission_amount}
    b_pk = b.pk
    b.delete()
    for name, value in avito_fields.items():
        setattr(a, name, value)

    for name, value in result.items():
        setattr(a, name, value)
    a.comment = "\n\n".join(comments)
    a.save()
    for data in link_data:
        LinkedEvent.objects.update_or_create(
            channel=data.pop("channel"), uid=data.pop("uid"), defaults={**data, "booking": a}
        )
    a.merged_from_pk = b_pk
    return a


def duplicate_pairs() -> list[tuple[Booking, Booking]]:
    """Пары действующих броней с полностью совпадающими датами — почти наверняка одна бронь."""
    return [(x, y) for x, y in conflict_pairs() if x.check_in == y.check_in and x.check_out == y.check_out]


def pair_key(x: Booking, y: Booking) -> str:
    lo, hi = sorted((x.pk, y.pk))
    return f"{lo}-{hi}"


# ---------------------------------------------------------------- страницы


def _choices_from_post(post) -> dict[str, str]:
    return {k[7:]: v for k, v in post.items() if k.startswith("choice_") and v in SIDES}


@login_required
def merge_view(request, a: int, b: int):
    qs = Booking.objects.select_related("channel", "guest").prefetch_related("payments")
    x, y = get_object_or_404(qs, pk=a), get_object_or_404(qs, pk=b)
    if x.pk == y.pk:
        messages.error(request, "Нельзя объединить бронь саму с собой")
        return redirect("booking_edit", pk=x.pk)
    if request.method == "POST":
        survivor = merge_bookings(x, y, _choices_from_post(request.POST), user=request.user)
        messages.success(request, "Брони объединены — дубля больше нет.")
        return redirect("booking_edit", pk=survivor.pk)
    plan = build_plan(x, y)
    return render(
        request,
        "booking/merge.html",
        {
            "plan": plan,
            "a": plan.a,
            "b": plan.b,
            "owner_sides": plan.owner_sides(),
            "defaults": plan.defaults(),
            "payments": {"a": list(plan.a.payments.all()), "b": list(plan.b.payments.all())},
        },
    )


@login_required
def duplicates_view(request):
    pairs = duplicate_pairs()
    if request.method == "POST":
        selected = set(request.POST.getlist("pair"))
        merged_into: dict[int, int] = {}  # удалённая бронь → в какую её объединили
        done = 0
        for x, y in pairs:
            if pair_key(x, y) not in selected:
                continue
            ids = []
            for pk in (x.pk, y.pk):
                while pk in merged_into:
                    pk = merged_into[pk]
                ids.append(pk)
            if ids[0] == ids[1]:
                continue
            first, second = Booking.objects.filter(pk=ids[0]).first(), Booking.objects.filter(pk=ids[1]).first()
            if first is None or second is None:
                continue
            survivor = merge_bookings(first, second, user=request.user)
            merged_into[survivor.merged_from_pk] = survivor.pk
            done += 1
        if done:
            messages.success(request, f"Объединено дублей: {done}")
        else:
            messages.info(request, "Ничего не выбрано")
        return redirect("duplicates" if duplicate_pairs() else "calendar")

    rows = []
    for x, y in pairs:
        plan = build_plan(x, y)
        rows.append({"key": pair_key(x, y), "plan": plan, "summary": plan.summary()})
    return render(request, "booking/duplicates.html", {"rows": rows})
